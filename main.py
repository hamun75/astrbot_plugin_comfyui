"""
AstrBot <-> ComfyUI Bridge

Sends text-to-image requests from AstrBot to a self-hosted ComfyUI
instance, using a workflow you export yourself from the ComfyUI UI
(menu: Save (API Format)). No assumptions about specific models,
checkpoints, or LoRAs are built in — the plugin just injects a prompt
into whichever node IDs you configure and returns whatever image
ComfyUI produces.

Two ways to trigger it:
  1. /draw <prompt>          — direct command, always available.
  2. Natural language        — via the `generate_image` LLM tool, so
                                the chat model can call it when a user
                                asks for a picture. Requires the model
                                provider to support tool/function calls.
"""

import asyncio
import json
import random
import time
import uuid
from pathlib import Path

import aiohttp

from astrbot.api import logger, llm_tool
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register


DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=60)


class ComfyUIError(Exception):
    """Raised whenever ComfyUI can't complete a generation request.

    The message is written to be shown directly to the end user, so
    keep it plain and actionable rather than a raw stack trace.
    """


@register(
    "astrbot_plugin_comfyui_bridge",
    "Melbit Services",
    "Bridges AstrBot to a self-hosted ComfyUI instance for text-to-image generation.",
    "1.0.0",
)
class ComfyUIBridge(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        server_address = str(self.config.get("server_address", "127.0.0.1:8188")).strip()
        if not server_address.startswith(("http://", "https://")):
            server_address = f"http://{server_address}"
        self.base_url = server_address.rstrip("/")

        self.workflow_file = self.config.get("workflow_file", "workflow_api.json")
        self.positive_node_id = str(self.config.get("positive_prompt_node_id", "6"))
        self.negative_node_id = str(self.config.get("negative_prompt_node_id", "") or "")
        self.output_node_id = str(self.config.get("output_node_id", "9"))
        self.seed_node_id = str(self.config.get("seed_node_id", "") or "")
        self.default_negative_prompt = self.config.get("default_negative_prompt", "")
        self.poll_timeout_seconds = int(self.config.get("poll_timeout_seconds", 120) or 120)

        plugin_dir = Path(__file__).parent
        self.workflow_dir = plugin_dir / "workflow"
        self.workflow_path = self.workflow_dir / self.workflow_file
        self.output_dir = plugin_dir / "output"
        self.output_dir.mkdir(exist_ok=True)

        logger.info(
            f"[ComfyUI Bridge] configured | server={self.base_url} | "
            f"workflow={self.workflow_file} | positive_node={self.positive_node_id} | "
            f"negative_node={self.negative_node_id or 'none'} | "
            f"output_node={self.output_node_id}"
        )

    async def terminate(self):
        """Nothing persistent to clean up — each request opens its own session."""
        pass

    # ------------------------------------------------------------------
    # Core ComfyUI plumbing
    # ------------------------------------------------------------------

    def _load_workflow(self) -> dict:
        if not self.workflow_path.exists():
            raise ComfyUIError(
                f"Workflow file not found: {self.workflow_path.name}. "
                f"Export it from ComfyUI (menu: Save (API Format)) and place it "
                f"in this plugin's workflow/ folder, then set workflow_file in "
                f"the plugin config to match."
            )
        with open(self.workflow_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _prepare_workflow(self, prompt: str, negative_prompt: str = "") -> dict:
        workflow = self._load_workflow()

        if self.positive_node_id not in workflow:
            raise ComfyUIError(
                f"positive_prompt_node_id '{self.positive_node_id}' was not found "
                f"in the workflow. Check the node ID in ComfyUI's developer mode."
            )
        workflow[self.positive_node_id].setdefault("inputs", {})["text"] = prompt

        if self.negative_node_id:
            if self.negative_node_id in workflow:
                neg_text = negative_prompt or self.default_negative_prompt
                workflow[self.negative_node_id].setdefault("inputs", {})["text"] = neg_text
            else:
                logger.warning(
                    f"[ComfyUI Bridge] negative_prompt_node_id "
                    f"'{self.negative_node_id}' not found in workflow, skipping"
                )

        seed_id = self.seed_node_id or self._guess_seed_node(workflow)
        if seed_id and seed_id in workflow:
            inputs = workflow[seed_id].setdefault("inputs", {})
            for key in ("seed", "noise_seed"):
                if key in inputs:
                    inputs[key] = random.randint(0, 2**32 - 1)

        return workflow

    @staticmethod
    def _guess_seed_node(workflow: dict):
        """Find the first node with a 'seed' or 'noise_seed' input, so
        repeated generations don't return the same cached image."""
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs", {})
            if "seed" in inputs or "noise_seed" in inputs:
                return node_id
        return None

    async def _queue_prompt(self, session: aiohttp.ClientSession, workflow: dict, client_id: str) -> str:
        payload = {"prompt": workflow, "client_id": client_id}
        async with session.post(f"{self.base_url}/prompt", json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise ComfyUIError(
                    f"ComfyUI rejected the request ({resp.status}). "
                    f"This is usually a wrong node ID. Details: {text[:300]}"
                )
            data = await resp.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyUIError(f"ComfyUI did not return a prompt_id: {data}")
        return prompt_id

    async def _wait_for_result(self, session: aiohttp.ClientSession, prompt_id: str) -> dict:
        deadline = time.monotonic() + self.poll_timeout_seconds
        while time.monotonic() < deadline:
            async with session.get(f"{self.base_url}/history/{prompt_id}") as resp:
                if resp.status == 200:
                    history = await resp.json()
                    entry = history.get(prompt_id)
                    if entry and entry.get("outputs"):
                        return entry
            await asyncio.sleep(1.5)
        raise ComfyUIError(
            f"Timed out after {self.poll_timeout_seconds}s waiting for ComfyUI "
            f"(prompt_id={prompt_id}). The queue may be busy — check the ComfyUI "
            f"dashboard, or raise poll_timeout_seconds in the plugin config."
        )

    def _extract_image_ref(self, history_entry: dict) -> dict:
        outputs = history_entry.get("outputs", {})
        node_output = outputs.get(self.output_node_id)
        if not node_output or not node_output.get("images"):
            # Fall back to the first node that produced any images, in
            # case the configured output_node_id is wrong.
            for node_id, data in outputs.items():
                if data.get("images"):
                    logger.warning(
                        f"[ComfyUI Bridge] output_node_id '{self.output_node_id}' "
                        f"had no images, using node '{node_id}' instead — "
                        f"consider fixing output_node_id in the plugin config"
                    )
                    node_output = data
                    break
        images = (node_output or {}).get("images", [])
        if not images:
            raise ComfyUIError(
                "ComfyUI finished the job but produced no images. Check that "
                "output_node_id points at a Save Image / Preview Image node."
            )
        return images[0]

    async def _download_image(self, session: aiohttp.ClientSession, image_ref: dict) -> bytes:
        params = {
            "filename": image_ref.get("filename"),
            "subfolder": image_ref.get("subfolder", ""),
            "type": image_ref.get("type", "output"),
        }
        async with session.get(f"{self.base_url}/view", params=params) as resp:
            if resp.status != 200:
                raise ComfyUIError(f"Failed to download the generated image ({resp.status})")
            return await resp.read()

    async def generate(self, prompt: str, negative_prompt: str = "") -> Path:
        """Run the full generate -> wait -> download cycle. Returns a local file path."""
        workflow = self._prepare_workflow(prompt, negative_prompt)
        client_id = str(uuid.uuid4())

        async with aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT) as session:
            prompt_id = await self._queue_prompt(session, workflow, client_id)
            logger.info(f"[ComfyUI Bridge] queued prompt_id={prompt_id}")
            history_entry = await self._wait_for_result(session, prompt_id)
            image_ref = self._extract_image_ref(history_entry)
            image_bytes = await self._download_image(session, image_ref)

        out_path = self.output_dir / f"{prompt_id}.png"
        out_path.write_bytes(image_bytes)
        return out_path

    # ------------------------------------------------------------------
    # AstrBot-facing entry points
    # ------------------------------------------------------------------

    @filter.command("draw")
    async def draw_command(self, event: AstrMessageEvent):
        """Generate an image with ComfyUI. Usage: /draw <prompt in English>"""
        prompt = event.message_str.split(maxsplit=1)
        prompt = prompt[1].strip() if len(prompt) > 1 else ""
        if not prompt:
            yield event.plain_result("Usage: /draw <prompt in English>")
            return

        yield event.plain_result("Generating your image — this can take a minute...")
        try:
            image_path = await self.generate(prompt)
        except ComfyUIError as e:
            logger.error(f"[ComfyUI Bridge] generation failed: {e}")
            yield event.plain_result(f"Image generation failed: {e}")
            return
        except Exception as e:
            logger.exception("[ComfyUI Bridge] unexpected error in /draw")
            yield event.plain_result(f"Unexpected error while generating the image: {e}")
            return

        yield event.image_result(str(image_path))

    @llm_tool("generate_image")
    async def generate_image_tool(
        self, event: AstrMessageEvent, prompt: str, negative_prompt: str = ""
    ) -> str:
        """Generate an image from a text description using the self-hosted
        ComfyUI/Flux setup. Use this whenever the user asks you to draw,
        create, generate, or make a picture or image.

        Args:
            prompt(string): A detailed English description of the image to generate.
            negative_prompt(string): Optional. Things to avoid in the image, in English.
        """
        # NOTE: this assumes your AstrBot version auto-injects the current
        # `event` into llm_tool calls when a parameter is named/typed as
        # AstrMessageEvent. If natural-language image requests silently do
        # nothing (check the logs for a TypeError about arguments), your
        # version may not support this — the /draw command above works
        # regardless of that, so it's the reliable fallback either way.
        try:
            image_path = await self.generate(prompt, negative_prompt)
        except ComfyUIError as e:
            return f"Image generation failed: {e}"
        except Exception as e:
            logger.exception("[ComfyUI Bridge] unexpected error in generate_image tool")
            return f"Unexpected error while generating the image: {e}"

        chain = MessageChain().file_image(str(image_path))
        await self.context.send_message(event.unified_msg_origin, chain)
        return "Image generated and sent to the user successfully."
