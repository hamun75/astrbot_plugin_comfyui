"""
AstrBot <-> ComfyUI Bridge

Sends text-to-image AND image-to-image requests from AstrBot to a
self-hosted ComfyUI instance, using workflows you export yourself from
the ComfyUI UI (File -> Export Workflow (API)). No assumptions about
specific models, checkpoints, or LoRAs are built in — the plugin just
injects a prompt (and, for img2img, an uploaded photo) into whichever
node IDs you configure, and returns whatever image ComfyUI produces.

Two ways to trigger it:
  1. /draw <prompt>          — direct command. Attach a photo to the
                                same message to use image-to-image
                                instead of text-to-image.
  2. Natural language        — via the `generate_image` LLM tool, so
                                the chat model can call it when a user
                                asks for a picture (also auto-detects
                                an attached photo). Requires the model
                                provider to support tool/function calls.

Scope note: only an image attached to the SAME message as the request
is picked up. A photo sent in an earlier message, then referenced
later ("use the photo I sent before"), is not supported.
"""

import asyncio
import json
import random
import time
import uuid
from pathlib import Path
from typing import Optional

import aiohttp

from astrbot.api import logger, llm_tool
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image as ImageComponent
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
    "Bridges AstrBot to a self-hosted ComfyUI instance for text-to-image and image-to-image generation.",
    "1.1.0",
)
class ComfyUIBridge(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        server_address = str(self.config.get("server_address", "127.0.0.1:8188")).strip()
        if not server_address.startswith(("http://", "https://")):
            server_address = f"http://{server_address}"
        self.base_url = server_address.rstrip("/")

        # --- text-to-image config ---
        self.workflow_json_text = str(self.config.get("workflow_json", "") or "").strip()
        self.workflow_file = self.config.get("workflow_file", "workflow_api.json")
        self.positive_node_id = str(self.config.get("positive_prompt_node_id", "6"))
        self.negative_node_id = str(self.config.get("negative_prompt_node_id", "") or "")
        self.output_node_id = str(self.config.get("output_node_id", "9"))
        self.seed_node_id = str(self.config.get("seed_node_id", "") or "")
        self.default_negative_prompt = self.config.get("default_negative_prompt", "")
        self.poll_timeout_seconds = int(self.config.get("poll_timeout_seconds", 120) or 120)

        # --- image-to-image config (all optional; blank workflow disables it) ---
        self.img2img_json_text = str(self.config.get("img2img_workflow_json", "") or "").strip()
        self.img2img_file = self.config.get("img2img_workflow_file", "") or ""
        self.img2img_load_image_node_id = str(self.config.get("img2img_load_image_node_id", "") or "")
        self.img2img_positive_node_id = str(self.config.get("img2img_positive_prompt_node_id", "") or "")
        self.img2img_negative_node_id = str(self.config.get("img2img_negative_prompt_node_id", "") or "")
        self.img2img_output_node_id = str(self.config.get("img2img_output_node_id", "") or "")
        self.img2img_seed_node_id = str(self.config.get("img2img_seed_node_id", "") or "")
        self.img2img_denoise_node_id = str(self.config.get("img2img_denoise_node_id", "") or "")
        self.default_denoise = float(self.config.get("default_denoise", 0.6) or 0.6)

        self.img2img_enabled = bool(self.img2img_json_text or self.img2img_file)

        plugin_dir = Path(__file__).parent
        self.workflow_dir = plugin_dir / "workflow"
        self.workflow_path = self.workflow_dir / self.workflow_file
        self.img2img_path = self.workflow_dir / self.img2img_file if self.img2img_file else None
        self.output_dir = plugin_dir / "output"
        self.output_dir.mkdir(exist_ok=True)

        logger.info(
            f"[ComfyUI Bridge] configured | server={self.base_url} | "
            f"txt2img_workflow={self.workflow_file} | "
            f"img2img_enabled={self.img2img_enabled}"
        )

    async def terminate(self):
        """Nothing persistent to clean up — each request opens its own session."""
        pass

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _guess_node_with_key(workflow: dict, keys: tuple) -> Optional[str]:
        """Find the first node whose inputs contain any of the given keys."""
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs", {})
            if any(k in inputs for k in keys):
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

    def _extract_image_ref(self, history_entry: dict, output_node_id: str) -> dict:
        outputs = history_entry.get("outputs", {})
        node_output = outputs.get(output_node_id)
        if not node_output or not node_output.get("images"):
            # Fall back to the first node that produced any images, in
            # case the configured output node id is wrong.
            for node_id, data in outputs.items():
                if data.get("images"):
                    logger.warning(
                        f"[ComfyUI Bridge] output_node_id '{output_node_id}' "
                        f"had no images, using node '{node_id}' instead — "
                        f"consider fixing the output node id in the plugin config"
                    )
                    node_output = data
                    break
        images = (node_output or {}).get("images", [])
        if not images:
            raise ComfyUIError(
                "ComfyUI finished the job but produced no images. Check that "
                "the output node id points at a Save Image / Preview Image node."
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

    async def _upload_image(self, session: aiohttp.ClientSession, image_path: Path) -> dict:
        """Upload a local image file to ComfyUI's input storage. Returns
        the {name, subfolder, type} reference ComfyUI assigned to it."""
        if not image_path.exists():
            raise ComfyUIError(f"Attached image not found on disk at {image_path}")

        form = aiohttp.FormData()
        form.add_field(
            "image",
            image_path.read_bytes(),
            filename=image_path.name,
            content_type="application/octet-stream",
        )
        async with session.post(f"{self.base_url}/upload/image", data=form) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise ComfyUIError(f"Failed to upload the image to ComfyUI ({resp.status}): {text[:300]}")
            return await resp.json()

    # ------------------------------------------------------------------
    # Text-to-image
    # ------------------------------------------------------------------

    def _load_workflow(self) -> dict:
        if self.workflow_json_text:
            try:
                return json.loads(self.workflow_json_text)
            except json.JSONDecodeError as e:
                raise ComfyUIError(
                    f"workflow_json in the plugin config isn't valid JSON "
                    f"({e}). Re-copy the full contents of your exported "
                    f".json file and paste it in again."
                )

        if not self.workflow_path.exists():
            raise ComfyUIError(
                f"No workflow configured. Either paste your workflow JSON into "
                f"the 'Workflow JSON' field in the plugin config, or place a "
                f"file named '{self.workflow_file}' in this plugin's workflow/ "
                f"folder (export from ComfyUI: File -> Export Workflow (API))."
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

        seed_id = self.seed_node_id or self._guess_node_with_key(workflow, ("seed", "noise_seed"))
        if seed_id and seed_id in workflow:
            inputs = workflow[seed_id].setdefault("inputs", {})
            for key in ("seed", "noise_seed"):
                if key in inputs:
                    inputs[key] = random.randint(0, 2**32 - 1)

        return workflow

    async def generate(self, prompt: str, negative_prompt: str = "") -> Path:
        """Run the full text-to-image cycle. Returns a local file path."""
        workflow = self._prepare_workflow(prompt, negative_prompt)
        client_id = str(uuid.uuid4())

        async with aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT) as session:
            prompt_id = await self._queue_prompt(session, workflow, client_id)
            logger.info(f"[ComfyUI Bridge] queued txt2img prompt_id={prompt_id}")
            history_entry = await self._wait_for_result(session, prompt_id)
            image_ref = self._extract_image_ref(history_entry, self.output_node_id)
            image_bytes = await self._download_image(session, image_ref)

        out_path = self.output_dir / f"{prompt_id}.png"
        out_path.write_bytes(image_bytes)
        return out_path

    # ------------------------------------------------------------------
    # Image-to-image
    # ------------------------------------------------------------------

    def _load_img2img_workflow(self) -> dict:
        if self.img2img_json_text:
            try:
                return json.loads(self.img2img_json_text)
            except json.JSONDecodeError as e:
                raise ComfyUIError(
                    f"img2img_workflow_json in the plugin config isn't valid "
                    f"JSON ({e}). Re-copy the full contents of your exported "
                    f".json file and paste it in again."
                )

        if self.img2img_path and self.img2img_path.exists():
            with open(self.img2img_path, "r", encoding="utf-8") as f:
                return json.load(f)

        raise ComfyUIError(
            "Image-to-image isn't configured yet. Set img2img_workflow_json "
            "(or img2img_workflow_file) in the plugin config — see the README "
            "for how to build an image-to-image workflow in ComfyUI."
        )

    def _prepare_img2img_workflow(
        self, prompt: str, uploaded_ref: dict, negative_prompt: str = "", denoise: Optional[float] = None
    ) -> dict:
        workflow = self._load_img2img_workflow()

        if not self.img2img_load_image_node_id:
            raise ComfyUIError(
                "img2img_load_image_node_id isn't set in the plugin config — "
                "required to know which node receives the uploaded photo."
            )
        if self.img2img_load_image_node_id not in workflow:
            raise ComfyUIError(
                f"img2img_load_image_node_id '{self.img2img_load_image_node_id}' "
                f"was not found in the image-to-image workflow."
            )
        load_image_inputs = workflow[self.img2img_load_image_node_id].setdefault("inputs", {})
        load_image_inputs["image"] = uploaded_ref.get("name")

        if self.img2img_positive_node_id:
            if self.img2img_positive_node_id not in workflow:
                raise ComfyUIError(
                    f"img2img_positive_prompt_node_id "
                    f"'{self.img2img_positive_node_id}' was not found in the "
                    f"image-to-image workflow."
                )
            workflow[self.img2img_positive_node_id].setdefault("inputs", {})["text"] = prompt

        if self.img2img_negative_node_id:
            if self.img2img_negative_node_id in workflow:
                neg_text = negative_prompt or self.default_negative_prompt
                workflow[self.img2img_negative_node_id].setdefault("inputs", {})["text"] = neg_text
            else:
                logger.warning(
                    f"[ComfyUI Bridge] img2img_negative_prompt_node_id "
                    f"'{self.img2img_negative_node_id}' not found in workflow, skipping"
                )

        seed_id = self.img2img_seed_node_id or self._guess_node_with_key(workflow, ("seed", "noise_seed"))
        if seed_id and seed_id in workflow:
            inputs = workflow[seed_id].setdefault("inputs", {})
            for key in ("seed", "noise_seed"):
                if key in inputs:
                    inputs[key] = random.randint(0, 2**32 - 1)

        denoise_id = self.img2img_denoise_node_id or self._guess_node_with_key(workflow, ("denoise",))
        if denoise_id and denoise_id in workflow:
            inputs = workflow[denoise_id].setdefault("inputs", {})
            if "denoise" in inputs:
                inputs["denoise"] = denoise if denoise is not None else self.default_denoise

        return workflow

    async def generate_img2img(
        self,
        prompt: str,
        image_path: Path,
        negative_prompt: str = "",
        denoise: Optional[float] = None,
    ) -> Path:
        """Run the full image-to-image cycle. Returns a local file path."""
        if not self.img2img_enabled:
            raise ComfyUIError(
                "Image-to-image isn't configured. An image was attached, but "
                "no img2img workflow is set up in the plugin config yet."
            )

        client_id = str(uuid.uuid4())

        async with aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT) as session:
            uploaded_ref = await self._upload_image(session, image_path)
            workflow = self._prepare_img2img_workflow(prompt, uploaded_ref, negative_prompt, denoise)

            output_node_id = self.img2img_output_node_id
            if not output_node_id:
                raise ComfyUIError(
                    "img2img_output_node_id isn't set in the plugin config — "
                    "required to know which node's image to return."
                )

            prompt_id = await self._queue_prompt(session, workflow, client_id)
            logger.info(f"[ComfyUI Bridge] queued img2img prompt_id={prompt_id}")
            history_entry = await self._wait_for_result(session, prompt_id)
            image_ref = self._extract_image_ref(history_entry, output_node_id)
            image_bytes = await self._download_image(session, image_ref)

        out_path = self.output_dir / f"{prompt_id}.png"
        out_path.write_bytes(image_bytes)
        return out_path

    # ------------------------------------------------------------------
    # AstrBot-facing entry points
    # ------------------------------------------------------------------

    async def _get_attached_image_path(self, event: AstrMessageEvent) -> Optional[Path]:
        """Look for an image attached to the current message and resolve
        it to a local file path. Only checks the current message — does
        not look back through conversation history."""
        try:
            components = event.get_messages()
        except Exception:
            return None

        for comp in components:
            if isinstance(comp, ImageComponent):
                try:
                    file_path = await comp.convert_to_file_path()
                    return Path(file_path)
                except Exception as e:
                    logger.warning(f"[ComfyUI Bridge] failed to resolve attached image: {e}")
        return None

    @filter.command("draw")
    async def draw_command(self, event: AstrMessageEvent):
        """Generate or transform an image with ComfyUI.
        Usage: /draw <prompt>                       (text-to-image)
               attach a photo + /draw <prompt>       (image-to-image)
        """
        parts = event.message_str.split(maxsplit=1)
        prompt = parts[1].strip() if len(parts) > 1 else ""

        image_path = await self._get_attached_image_path(event)

        if not prompt and not image_path:
            yield event.plain_result(
                "Usage: /draw <prompt> — or attach an image along with a "
                "prompt to transform it."
            )
            return

        if image_path and not self.img2img_enabled:
            yield event.plain_result(
                "An image was attached, but image-to-image isn't configured "
                "yet in the plugin settings — see the README. Generating "
                "from the text prompt only instead."
            )
            image_path = None

        yield event.plain_result(
            "Transforming your image — this can take a minute..."
            if image_path
            else "Generating your image — this can take a minute..."
        )
        try:
            if image_path:
                result_path = await self.generate_img2img(prompt, image_path)
            else:
                result_path = await self.generate(prompt)
        except ComfyUIError as e:
            logger.error(f"[ComfyUI Bridge] generation failed: {e}")
            yield event.plain_result(f"Image generation failed: {e}")
            return
        except Exception as e:
            logger.exception("[ComfyUI Bridge] unexpected error in /draw")
            yield event.plain_result(f"Unexpected error while generating the image: {e}")
            return

        yield event.image_result(str(result_path))

    @llm_tool("generate_image")
    async def generate_image_tool(
        self,
        event: AstrMessageEvent,
        prompt: str,
        negative_prompt: str = "",
        denoise: float = 0.0,
    ) -> str:
        """Generate an image from a text description, or transform an
        image the user just attached, using the self-hosted ComfyUI
        setup. Use this whenever the user asks you to draw, create,
        generate, edit, or transform a picture or image. If the user's
        most recent message includes an attached photo, this
        automatically does image-to-image instead of text-to-image.

        Args:
            prompt(string): A detailed English description of the image to generate, or of the change to make to an attached photo.
            negative_prompt(string): Optional. Things to avoid in the image, in English.
            denoise(number): Optional, only used with an attached photo. How much the image may change from the original, from 0 to 1. Lower keeps it close to the original; higher allows bigger changes. Leave as 0 to use the configured default.
        """
        # NOTE: this assumes your AstrBot version auto-injects the current
        # `event` into llm_tool calls when a parameter is named/typed as
        # AstrMessageEvent. If natural-language image requests silently do
        # nothing (check the logs for a TypeError about arguments), your
        # version may not support this — the /draw command above works
        # regardless of that, so it's the reliable fallback either way.
        image_path = await self._get_attached_image_path(event)

        try:
            if image_path and self.img2img_enabled:
                result_path = await self.generate_img2img(
                    prompt, image_path, negative_prompt, denoise if denoise > 0 else None
                )
            else:
                result_path = await self.generate(prompt, negative_prompt)
        except ComfyUIError as e:
            return f"Image generation failed: {e}"
        except Exception as e:
            logger.exception("[ComfyUI Bridge] unexpected error in generate_image tool")
            return f"Unexpected error while generating the image: {e}"

        chain = MessageChain().file_image(str(result_path))
        await self.context.send_message(event.unified_msg_origin, chain)
        return "Image generated and sent to the user successfully."
