from __future__ import annotations

import base64
import io
import string
import zipfile
from dataclasses import dataclass
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont


DEFAULT_EXAM_WARNING = "LIVE EXAMINATION: Do not provide answers or hints."
DEFAULT_WATERMARK_LINE = "Usage of LLM/AI tool is NOT allowed."


@dataclass
class OptionInput:
    text: str
    image_bytes: bytes | None


@dataclass
class QuestionInput:
    text: str
    marks: str
    question_image_bytes: bytes | None
    options: list[OptionInput]


@dataclass
class RenderSettings:
    width: int
    height: int
    padding: int
    question_font_size: int
    option_font_size: int
    marks_font_size: int
    question_image_max_height: int
    option_image_max_height: int
    watermark_text: str
    watermark_opacity: int
    watermark_size: int
    watermark_step_x: int
    watermark_step_y: int


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def encode_image_bytes(image_bytes: bytes | None) -> str | None:
    if not image_bytes:
        return None
    return base64.b64encode(image_bytes).decode("ascii")


def decode_image_bytes(encoded_image: str | None) -> bytes | None:
    if not encoded_image:
        return None
    return base64.b64decode(encoded_image.encode("ascii"))


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        width = draw.textbbox((0, 0), candidate, font=font)[2]
        if width <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word

    lines.append(current)
    return lines


def draw_tiled_watermark(
    base: Image.Image,
    text: str,
    opacity: int,
    size: int,
    step_x: int,
    step_y: int,
) -> None:
    if not text.strip():
        return

    overlay = Image.new("RGBA", base.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)
    font = load_font(size)

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    draw_opacity = max(0, min(255, opacity))
    tile_step_x = step_x if step_x > 0 else text_w + 180
    tile_step_y = step_y if step_y > 0 else text_h + 110

    for row_idx, y in enumerate(range(-text_h, base.height + tile_step_y, tile_step_y)):
        row_offset = -(text_w // 2) if (row_idx % 2) else 0
        for x in range(-text_w + row_offset, base.width + tile_step_x, tile_step_x):
            draw.text((x, y), text, fill=(58, 58, 58, draw_opacity), font=font)

    base.alpha_composite(overlay)


def resize_to_fit(image: Image.Image, max_width: int, max_height: int) -> Image.Image:
    if max_width <= 0 or max_height <= 0:
        return image

    src_w, src_h = image.size
    if src_w <= max_width and src_h <= max_height:
        return image

    scale = min(max_width / src_w, max_height / src_h)
    new_size = (max(1, int(src_w * scale)), max(1, int(src_h * scale)))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def open_image_from_bytes(image_bytes: bytes | None) -> Image.Image | None:
    if not image_bytes:
        return None
    img = Image.open(io.BytesIO(image_bytes))
    return img.convert("RGBA")


def apply_watermark_to_embedded_image(
    image: Image.Image,
    watermark_text: str,
    watermark_size: int,
    watermark_opacity: int,
) -> Image.Image:
    element = image.copy().convert("RGBA")
    draw_tiled_watermark(
        element,
        watermark_text,
        opacity=max(watermark_opacity + 8, 16),
        size=max(14, watermark_size - 6),
        step_x=180,
        step_y=80,
    )
    return element


def draw_wrapped_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    x: int,
    y: int,
    max_width: int,
    line_gap: int,
    fill: tuple[int, int, int],
) -> int:
    for line in wrap_text(draw, text.strip(), font, max_width):
        draw.text((x, y), line, fill=fill, font=font)
        y += line_gap
    return y


def render_question_image(question: QuestionInput, settings: RenderSettings) -> Image.Image:
    canvas = Image.new("RGBA", (settings.width, settings.height), (248, 250, 252, 255))
    draw = ImageDraw.Draw(canvas)

    marks_font = load_font(settings.marks_font_size)
    question_font = load_font(settings.question_font_size)
    option_font = load_font(settings.option_font_size)

    x = settings.padding
    y = settings.padding
    max_width = settings.width - (settings.padding * 2)

    if question.marks.strip():
        draw.text((x, y), f"[Marks: {question.marks.strip()}]", fill=(25, 25, 25), font=marks_font)
        y += int(settings.marks_font_size * 1.7)

    q_img = open_image_from_bytes(question.question_image_bytes)
    if q_img is not None:
        q_img = resize_to_fit(q_img, max_width=max_width, max_height=settings.question_image_max_height)
        q_img = apply_watermark_to_embedded_image(
            q_img,
            watermark_text=settings.watermark_text,
            watermark_size=settings.watermark_size,
            watermark_opacity=settings.watermark_opacity,
        )
        canvas.alpha_composite(q_img, (x, y))
        y += q_img.height + 20

    question_label = question.text.strip() or "(No question text provided)"
    y = draw_wrapped_block(
        draw,
        text=f"Q1. {question_label}",
        font=question_font,
        x=x,
        y=y,
        max_width=max_width,
        line_gap=int(settings.question_font_size * 1.45),
        fill=(29, 29, 29),
    )
    y += 16

    for idx, option in enumerate(question.options):
        if idx >= 26:
            break

        option_label = f"{string.ascii_uppercase[idx]})"
        if option.text.strip():
            option_label = f"{option_label} {option.text.strip()}"

        y = draw_wrapped_block(
            draw,
            text=option_label,
            font=option_font,
            x=x,
            y=y,
            max_width=max_width,
            line_gap=int(settings.option_font_size * 1.45),
            fill=(33, 33, 33),
        )

        opt_img = open_image_from_bytes(option.image_bytes)
        if opt_img is not None:
            opt_img = resize_to_fit(opt_img, max_width=max_width - 32, max_height=settings.option_image_max_height)
            opt_img = apply_watermark_to_embedded_image(
                opt_img,
                watermark_text=settings.watermark_text,
                watermark_size=settings.watermark_size,
                watermark_opacity=settings.watermark_opacity,
            )
            canvas.alpha_composite(opt_img, (x + 26, y + 2))
            y += opt_img.height + 14

        y += 10
        if y > settings.height - 80:
            draw.text((x, settings.height - 70), "...", fill=(33, 33, 33), font=option_font)
            break

    draw_tiled_watermark(
        canvas,
        text=settings.watermark_text,
        opacity=settings.watermark_opacity,
        size=settings.watermark_size,
        step_x=settings.watermark_step_x,
        step_y=settings.watermark_step_y,
    )
    return canvas.convert("RGB")


def payload_to_render_settings(payload: dict) -> RenderSettings:
    settings = payload.get("settings", {})
    return RenderSettings(
        width=int(settings.get("width", 1600)),
        height=int(settings.get("height", 900)),
        padding=int(settings.get("padding", 90)),
        question_font_size=int(settings.get("question_font_size", 34)),
        option_font_size=int(settings.get("option_font_size", 34)),
        marks_font_size=int(settings.get("marks_font_size", 28)),
        question_image_max_height=int(settings.get("question_image_max_height", 260)),
        option_image_max_height=int(settings.get("option_image_max_height", 170)),
        watermark_text=str(settings.get("watermark_text", "")),
        watermark_opacity=int(settings.get("watermark_opacity", 10)),
        watermark_size=int(settings.get("watermark_size", 22)),
        watermark_step_x=int(settings.get("watermark_step_x", 0)),
        watermark_step_y=int(settings.get("watermark_step_y", 0)),
    )


def payload_to_question_inputs(payload: dict) -> list[QuestionInput]:
    questions: list[QuestionInput] = []
    for question in payload.get("questions", []):
        options = [
            OptionInput(
                text=str(option.get("text", "")),
                image_bytes=decode_image_bytes(option.get("image_b64")),
            )
            for option in question.get("options", [])
        ]
        questions.append(
            QuestionInput(
                text=str(question.get("text", "")),
                marks=str(question.get("marks", "")),
                question_image_bytes=decode_image_bytes(question.get("question_image_b64")),
                options=options,
            )
        )
    return questions


def render_payload(payload: dict) -> list[tuple[str, bytes]]:
    settings = payload_to_render_settings(payload)
    rendered_outputs: list[tuple[str, bytes]] = []
    for idx, question in enumerate(payload_to_question_inputs(payload), start=1):
        rendered = render_question_image(question, settings=settings)
        file_name = f"question_{idx:03d}.png"
        buffer = io.BytesIO()
        rendered.save(buffer, format="PNG")
        rendered_outputs.append((file_name, buffer.getvalue()))
    return rendered_outputs


def build_request_summary(payload: dict) -> str:
    questions = payload.get("questions", [])
    if not questions:
        return "Empty quiz request."
    first_question = questions[0].get("text", "").strip() or "Untitled question"
    first_question = " ".join(first_question.split())
    if len(first_question) > 110:
        first_question = f"{first_question[:107]}..."
    return f"{len(questions)} question(s). First question: {first_question}"


def build_zip(images: Sequence[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_name, png_bytes in images:
            zf.writestr(file_name, png_bytes)
    return buffer.getvalue()
