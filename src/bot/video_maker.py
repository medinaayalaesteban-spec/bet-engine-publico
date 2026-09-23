"""Audiovisual automation - render a TikTok script into a vertical .mp4.

  generate_audio(text, output_path)         -> narrates the script to .mp3 (edge-tts)
  render_tiktok_video(audio, image, out)    -> static image + audio -> .mp4 (moviepy)

Voice: es-MX-JorgeNeural (neutral Latin-American Spanish male). Override with
env TTS_VOICE. List all voices with:  python -m edge_tts --list-voices
"""

# ---------------------------------------------------------------------------
# FUTURE: dynamic AI backgrounds via a local Stable Diffusion (AUTOMATIC1111)
# server instead of a fixed assets/background.jpg. Sketch only - not wired up.
#
# import base64, requests
#
# def generate_background(prompt, output_path,
#                         url="http://127.0.0.1:7860/sdapi/v1/txt2img"):
#     payload = {
#         "prompt": prompt,                      # e.g. "cinematic football stadium
#                                                #       at night, dramatic lighting,
#                                                #       vertical composition"
#         "negative_prompt": "text, watermark, logo, blurry",
#         "width": 1080,
#         "height": 1920,                        # native vertical -> no cropping
#         "steps": 30,
#         "cfg_scale": 7,
#         "sampler_name": "DPM++ 2M Karras",
#     }
#     r = requests.post(url, json=payload, timeout=300)
#     r.raise_for_status()
#     img_b64 = r.json()["images"][0]
#     with open(output_path, "wb") as fh:
#         fh.write(base64.b64decode(img_b64.split(",", 1)[-1]))
#     return output_path
#
# Then in render_tiktok_video(): call generate_background(<hook line>, tmp_path)
# and use tmp_path as image_path when no static background is supplied.
# ---------------------------------------------------------------------------

import asyncio
import os
import re

TTS_VOICE = os.getenv("TTS_VOICE", "es-MX-JorgeNeural")
VIDEO_SIZE = (1080, 1920)  # vertical / portrait, TikTok-native
VIDEO_FPS = int(os.getenv("VIDEO_FPS", "24"))


def _clean_script_for_tts(text):
    """Strip the structural labels ([GANCHO]/[AUTORIDAD]/[CIERRE], or any other
    bracketed tag) so the voice reads only the spoken lines. Keeps line breaks
    as natural pauses.
    """
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Drop lines that are just a bracketed label, e.g. "[GANCHO]".
        if re.fullmatch(r"\[[^\]]+\]", line):
            continue
        # Drop an inline leading label, e.g. "[GANCHO] Las casas de apuestas...".
        line = re.sub(r"^\[[^\]]+\]\s*", "", line)
        if line:
            lines.append(line)
    return "\n".join(lines)


def generate_audio(text, output_path):
    """Convert `text` (a TikTok script) to an .mp3 at `output_path` using
    edge-tts and the configured Spanish voice. Returns output_path.
    """
    spoken = _clean_script_for_tts(text)
    if not spoken.strip():
        raise ValueError("Nothing to narrate after cleaning the script.")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    import edge_tts  # imported lazily so the module loads without it installed

    async def _synthesize():
        communicate = edge_tts.Communicate(spoken, TTS_VOICE)
        await communicate.save(output_path)

    try:
        asyncio.run(_synthesize())
    except RuntimeError:
        # An event loop is already running (e.g. inside a notebook).
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_synthesize())
        finally:
            loop.close()
    return output_path


def render_tiktok_video(audio_path, image_path, output_mp4,
                        size=VIDEO_SIZE, fps=VIDEO_FPS):
    """Compile a static-image + narration .mp4 the exact length of the audio.

    `image_path` should be a 1080x1920 vertical background; it is force-resized
    to `size` so the output is always TikTok-native portrait.
    """
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Background image not found: {image_path}")
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    os.makedirs(os.path.dirname(os.path.abspath(output_mp4)), exist_ok=True)

    from moviepy import AudioFileClip, ImageClip

    audio = AudioFileClip(audio_path)
    clip = (
        ImageClip(image_path)
        .resized(size)
        .with_duration(audio.duration)   # video lasts exactly as long as the audio
        .with_audio(audio)
        .with_fps(fps)
    )
    clip.write_videofile(
        output_mp4,
        codec="libx264",
        audio_codec="aac",
        fps=fps,
        logger=None,
    )
    clip.close()
    audio.close()
    return output_mp4


def make_tiktok_video(script_text, image_path, out_basepath):
    """Convenience: script -> narration .mp3 + final .mp4 sharing a basename.

    `out_basepath` is a path without extension, e.g. 'output/cagliari_vs_lecce'.
    Returns (mp3_path, mp4_path).
    """
    mp3_path = f"{out_basepath}.mp3"
    mp4_path = f"{out_basepath}.mp4"
    generate_audio(script_text, mp3_path)
    render_tiktok_video(mp3_path, image_path, mp4_path)
    return mp3_path, mp4_path


if __name__ == "__main__":
    demo = (
        "[GANCHO]\nLos corredores de apuestas se equivocaron con este partido.\n"
        "[AUTORIDAD]\nNuestro modelo Poisson da un cuarenta y ocho por ciento al "
        "local, y la casa paga como si fuera un treinta.\n"
        "[CIERRE]\nToca el enlace en la biografia y unete gratis al Telegram. "
        "Apuesta con responsabilidad. Mayores de 18."
    )
    base = os.path.join(os.path.dirname(__file__), "..", "..", "output", "video_maker_demo")
    img = os.path.join(os.path.dirname(__file__), "..", "..", "assets", "background.jpg")
    print("Generating demo audio + video...")
    mp3, mp4 = make_tiktok_video(demo, img, base)
    print("wrote", mp3)
    print("wrote", mp4)
