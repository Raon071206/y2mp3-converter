"""Core YouTube & SoundCloud audio/thumbnail extraction engine."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PIL import Image
import yt_dlp


YOUTUBE_URL_PATTERN = re.compile(
    r"^https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/.+$",
    re.IGNORECASE,
)
SOUNDCLOUD_URL_PATTERN = re.compile(
    r"^https?://(www\.|m\.|on\.)?soundcloud\.com/.+$",
    re.IGNORECASE,
)

LOSSLESS_FORMATS = {"wav", "flac"}
THUMBNAIL_SUPPORTED_FORMATS = {"mp3", "m4a", "flac", "ogg"}


class DownloadCancelled(Exception):
    """Raised when the user cancels an active download."""


@dataclass(frozen=True)
class ConversionOptions:
    """Configuration for an audio conversion batch job."""

    urls: list[str]
    output_dir: Path
    audio_format: str = "mp3"  # "mp3" | "m4a" | "wav" | "flac"
    bitrate: str = "192"
    allow_playlist: bool = False
    embed_metadata: bool = True
    embed_thumbnail: bool = False
    start_time: str = ""
    end_time: str = ""
    concurrent_fragments: int = 4


@dataclass(frozen=True)
class ThumbnailOptions:
    """Configuration for extracting thumbnail/artwork images."""

    urls: list[str]
    output_dir: Path
    quality: str = "best"  # "best" | "maxresdefault" | "sddefault" | "hqdefault" | "mqdefault"
    allow_playlist: bool = False


@dataclass(frozen=True)
class ProgressEvent:
    """Thread-safe progress payload emitted to the UI."""

    kind: str  # "status" | "progress" | "done" | "error" | "cancelled"
    message: str
    percent: float = 0.0
    title: str = ""
    speed_text: str = ""
    eta_text: str = ""


def get_resource_dir() -> Path:
    """Return directory containing bundled read-only resources (PyInstaller-safe)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def get_app_dir() -> Path:
    """Return writable directory next to the executable or script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_ffmpeg_dir() -> Optional[Path]:
    """Return the directory containing ffmpeg.exe (bundled ./bin first, then PATH)."""
    for candidate_root in (get_resource_dir(), get_app_dir()):
        bundled_bin = candidate_root / "bin"
        if (bundled_bin / "ffmpeg.exe").is_file():
            return bundled_bin

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return Path(system_ffmpeg).resolve().parent

    return None


def detect_platform(url: str) -> str:
    """Return 'YouTube', 'SoundCloud', or '' if unsupported."""
    cleaned = url.strip()
    if YOUTUBE_URL_PATTERN.match(cleaned):
        return "YouTube"
    if SOUNDCLOUD_URL_PATTERN.match(cleaned):
        return "SoundCloud"
    return ""


def parse_url_lines(raw_text: str) -> list[str]:
    """Split multi-line input into non-empty stripped URLs."""
    return [line.strip() for line in raw_text.splitlines() if line.strip()]


def parse_time_to_seconds(value: str) -> Optional[float]:
    """Parse 'MM:SS:mmm', 'HH:MM:SS:mmm', 'HH:MM:SS', 'MM:SS', or 'SS' into seconds."""
    text = value.strip()
    if not text:
        return None

    parts = text.split(":")
    try:
        if len(parts) == 4:
            # HH:MM:SS:mmm
            hh, mm, ss, ms = int(parts[0]), int(parts[1]), float(parts[2]), int(parts[3])
            if any(v < 0 for v in (hh, mm, ss, ms)) or ms >= 1000:
                raise ValueError
            return hh * 3600.0 + mm * 60.0 + ss + ms / 1000.0
        if len(parts) == 3:
            # Distinguish MM:SS:mmm (3-digit ms) vs HH:MM:SS
            if len(parts[2]) == 3 and parts[2].isdigit():
                mm, ss, ms = int(parts[0]), float(parts[1]), int(parts[2])
                if mm < 0 or ss < 0 or ms < 0:
                    raise ValueError
                return mm * 60.0 + ss + ms / 1000.0
            hh, mm, ss = int(parts[0]), int(parts[1]), float(parts[2])
            if hh < 0 or mm < 0 or ss < 0:
                raise ValueError
            return hh * 3600.0 + mm * 60.0 + ss
        if len(parts) == 2:
            mm, ss = int(parts[0]), float(parts[1])
            if mm < 0 or ss < 0:
                raise ValueError
            return mm * 60.0 + ss
        if len(parts) == 1:
            ss = float(parts[0])
            if ss < 0:
                raise ValueError
            return ss
    except ValueError as exc:
        raise ValueError(f"잘못된 시간 형식입니다 (예: 01:23:500): {value}") from exc

    raise ValueError(f"잘못된 시간 형식입니다 (예: 01:23:500): {value}")


def format_seconds_ms(seconds: float) -> str:
    """Format seconds as 'MM:SS:mmm'."""
    total_ms = max(0, int(round(seconds * 1000)))
    mins, rem_ms = divmod(total_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{mins:02d}:{secs:02d}:{ms:03d}"


def get_audio_duration(file_path: Path) -> Optional[float]:
    """Detect audio file duration in seconds via ffprobe (or mutagen fallback)."""
    if not file_path.is_file():
        return None

    ffmpeg_dir = find_ffmpeg_dir()
    if ffmpeg_dir is not None:
        ffprobe = ffmpeg_dir / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
        if ffprobe.is_file():
            cmd = [
                str(ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ]
            kwargs: dict = {"capture_output": True, "text": True}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            res = subprocess.run(cmd, **kwargs)
            if res.returncode == 0 and res.stdout.strip():
                try:
                    return float(res.stdout.strip())
                except ValueError:
                    pass

    try:
        import mutagen

        audio = mutagen.File(str(file_path))
        if audio is not None and audio.info is not None:
            return float(audio.info.length)
    except Exception:
        pass
    return None


def _sanitize_filename(name: str) -> str:
    """Replace Windows-invalid filename characters."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return cleaned or "audio"


def _unique_path(path: Path) -> Path:
    """Append ' (1)', ' (2)', ... if target file already exists."""
    if not path.exists():
        return path
    idx = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({idx}){path.suffix}")
        if not candidate.exists():
            return candidate
        idx += 1


def _quality_label(audio_format: str, bitrate: str) -> str:
    if audio_format in LOSSLESS_FORMATS:
        return "무손실"
    return f"{bitrate}kbps"


def _format_speed(speed_bytes: Optional[float]) -> str:
    if not speed_bytes or speed_bytes <= 0:
        return ""
    if speed_bytes >= 1024 * 1024:
        return f"{speed_bytes / (1024 * 1024):.1f} MB/s"
    return f"{speed_bytes / 1024:.0f} KB/s"


def _format_eta(eta_seconds: Optional[float]) -> str:
    if eta_seconds is None or eta_seconds < 0:
        return ""
    mins, secs = divmod(int(eta_seconds), 60)
    if mins > 0:
        return f"{mins}분 {secs}초 남음"
    return f"{secs}초 남음"


def _run_ffmpeg_trim(
    ffmpeg_dir: Path,
    src_path: Path,
    dest_path: Path,
    start_sec: Optional[float],
    end_sec: Optional[float],
) -> None:
    """Trim src_path into dest_path using ffmpeg.exe with accurate input timestamps."""
    ffmpeg_exe = ffmpeg_dir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if not ffmpeg_exe.is_file():
        ffmpeg_exe = Path(shutil.which("ffmpeg") or "ffmpeg")

    cmd = [str(ffmpeg_exe), "-y", "-i", str(src_path)]
    if start_sec is not None:
        cmd.extend(["-ss", f"{start_sec:.3f}"])
    if end_sec is not None:
        cmd.extend(["-to", f"{end_sec:.3f}"])
    cmd.extend(["-c", "copy", str(dest_path)])

    kwargs: dict = {"capture_output": True, "text": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0 or not dest_path.is_file():
        raise RuntimeError(f"구간 자르기 실패: {result.stderr.strip()}")


def _trim_audio_file(
    ffmpeg_dir: Path,
    src_path: Path,
    start_sec: Optional[float],
    end_sec: Optional[float],
) -> None:
    """Trim an audio file in-place using ffmpeg.exe."""
    trimmed_path = src_path.with_name(f"trimmed_{src_path.name}")
    _run_ffmpeg_trim(ffmpeg_dir, src_path, trimmed_path, start_sec, end_sec)
    trimmed_path.replace(src_path)


def convert_to_mp3(
    options: ConversionOptions,
    emit: Callable[[ProgressEvent], None],
    cancel_event: threading.Event,
) -> None:
    """Download audio from one or more URLs and save as '<title>(<quality>).<ext>'."""
    urls = [u.strip() for u in options.urls if u.strip()]
    if not urls:
        emit(ProgressEvent(kind="error", message="URL을 하나 이상 입력해 주세요."))
        return

    invalid = [u for u in urls if not detect_platform(u)]
    if invalid:
        emit(
            ProgressEvent(
                kind="error",
                message=f"지원하지 않는 URL이 있습니다: {invalid[0]}",
            )
        )
        return

    try:
        start_sec = parse_time_to_seconds(options.start_time)
        end_sec = parse_time_to_seconds(options.end_time)
        if start_sec is not None and end_sec is not None and start_sec >= end_sec:
            emit(ProgressEvent(kind="error", message="종료 시간은 시작 시간보다 커야 합니다."))
            return
    except ValueError as exc:
        emit(ProgressEvent(kind="error", message=str(exc)))
        return

    ffmpeg_dir = find_ffmpeg_dir()
    if ffmpeg_dir is None:
        emit(
            ProgressEvent(
                kind="error",
                message="FFmpeg을 찾을 수 없습니다. bin 폴더 또는 시스템 PATH를 확인해 주세요.",
            )
        )
        return

    options.output_dir.mkdir(parents=True, exist_ok=True)
    fmt = options.audio_format.lower()
    use_embed_thumb = options.embed_thumbnail and (fmt in THUMBNAIL_SUPPORTED_FORMATS)
    quality_tag = _quality_label(fmt, options.bitrate)
    need_trim = start_sec is not None or end_sec is not None

    total_urls = len(urls)
    saved_count = 0

    for url_idx, url in enumerate(urls, start=1):
        if cancel_event.is_set():
            emit(ProgressEvent(kind="cancelled", message="작업이 취소되었습니다."))
            return

        platform = detect_platform(url)
        prefix = f"[{url_idx}/{total_urls}] " if total_urls > 1 else ""
        current_title = ""
        last_reported_pct = -1.0

        def progress_hook(d: dict) -> None:
            nonlocal current_title, last_reported_pct
            if cancel_event.is_set():
                raise DownloadCancelled("사용자가 작업을 취소했습니다.")

            info = d.get("info_dict") or {}
            if info.get("title"):
                current_title = str(info["title"])

            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                pct = (downloaded / total) if total > 0 else 0.0
                pct = max(0.0, min(1.0, pct))

                if abs(pct - last_reported_pct) >= 0.01 or pct >= 0.999:
                    last_reported_pct = pct
                    overall = ((url_idx - 1) + pct * 0.85) / total_urls
                    emit(
                        ProgressEvent(
                            kind="progress",
                            message=f"{prefix}다운로드 중... ({pct * 100:.0f}%)",
                            percent=overall,
                            title=current_title,
                            speed_text=_format_speed(d.get("speed")),
                            eta_text=_format_eta(d.get("eta")),
                        )
                    )
            elif status == "finished":
                overall = ((url_idx - 1) + 0.88) / total_urls
                emit(
                    ProgressEvent(
                        kind="status",
                        message=f"{prefix}{fmt.upper()} 변환 중...",
                        percent=overall,
                        title=current_title,
                    )
                )

        def postprocessor_hook(d: dict) -> None:
            if cancel_event.is_set():
                raise DownloadCancelled("사용자가 작업을 취소했습니다.")

        codec = "vorbis" if fmt == "ogg" else fmt
        postprocessors: list[dict] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec,
                "preferredquality": "0" if fmt in LOSSLESS_FORMATS else options.bitrate,
            }
        ]
        if use_embed_thumb:
            postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
        if options.embed_metadata:
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
        if use_embed_thumb:
            postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

        with tempfile.TemporaryDirectory(prefix="y2mp3_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            ydl_opts: dict = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(tmp_dir_str, "%(title)s.%(ext)s"),
                "ffmpeg_location": str(ffmpeg_dir),
                "noplaylist": not options.allow_playlist,
                "writethumbnail": use_embed_thumb,
                "concurrent_fragment_downloads": options.concurrent_fragments,
                "postprocessors": postprocessors,
                "progress_hooks": [progress_hook],
                "postprocessor_hooks": [postprocessor_hook],
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "retries": 3,
                "fragment_retries": 3,
            }

            try:
                emit(
                    ProgressEvent(
                        kind="status",
                        message=f"{prefix}{platform} 정보 분석 중...",
                        percent=((url_idx - 1) + 0.05) / total_urls,
                    )
                )
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if info and not current_title:
                        current_title = str(info.get("title", ""))

                if cancel_event.is_set():
                    emit(ProgressEvent(kind="cancelled", message="작업이 취소되었습니다."))
                    return

                produced = sorted(tmp_dir.glob(f"*.{fmt}"))
                for audio_file in produced:
                    if need_trim:
                        emit(
                            ProgressEvent(
                                kind="status",
                                message=f"{prefix}구간 자르기 처리 중...",
                                percent=((url_idx - 1) + 0.95) / total_urls,
                                title=audio_file.stem,
                            )
                        )
                        _trim_audio_file(ffmpeg_dir, audio_file, start_sec, end_sec)

                    safe_stem = _sanitize_filename(audio_file.stem)
                    dest = _unique_path(
                        options.output_dir / f"{safe_stem}({quality_tag}).{fmt}"
                    )
                    shutil.move(str(audio_file), str(dest))
                    saved_count += 1

            except DownloadCancelled:
                emit(ProgressEvent(kind="cancelled", message="작업이 취소되었습니다."))
                return
            except yt_dlp.utils.DownloadError as exc:
                emit(ProgressEvent(kind="error", message=f"{prefix}다운로드 오류: {exc}"))
                return
            except Exception as exc:
                emit(ProgressEvent(kind="error", message=f"{prefix}오류: {exc}"))
                return

    emit(
        ProgressEvent(
            kind="done",
            message=f"총 {saved_count}개 {fmt.upper()} 파일 저장 완료!",
            percent=1.0,
            title=current_title,
        )
    )


def _download_image_bytes(url: str) -> Optional[bytes]:
    """Download image bytes from URL; return None on HTTP/network failure."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
    return None


def _pick_thumbnail_candidates(entry: dict, platform: str, quality: str) -> list[str]:
    """Build an ordered list of candidate thumbnail URLs for an entry."""
    candidates: list[str] = []
    vid = entry.get("id")

    if platform == "YouTube" and vid:
        if quality == "best":
            candidates.extend(
                [
                    f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
                    f"https://i.ytimg.com/vi/{vid}/sddefault.jpg",
                    f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                ]
            )
        else:
            return [f"https://i.ytimg.com/vi/{vid}/{quality}.jpg"]

    thumbs = entry.get("thumbnails") or []
    sorted_thumbs = sorted(
        (t for t in thumbs if t.get("url")),
        key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
        reverse=True,
    )
    for t in sorted_thumbs:
        u = str(t["url"])
        if u not in candidates:
            candidates.append(u)

    if entry.get("thumbnail"):
        u = str(entry["thumbnail"])
        if u not in candidates:
            candidates.append(u)

    return candidates


def extract_thumbnail(
    options: ThumbnailOptions,
    emit: Callable[[ProgressEvent], None],
    cancel_event: threading.Event,
) -> None:
    """Extract and save thumbnail image(s) as '<title>(<WxH>).jpg' with duplicate safety."""
    urls = [u.strip() for u in options.urls if u.strip()]
    if not urls:
        emit(ProgressEvent(kind="error", message="URL을 하나 이상 입력해 주세요."))
        return

    invalid = [u for u in urls if not detect_platform(u)]
    if invalid:
        emit(
            ProgressEvent(
                kind="error",
                message=f"지원하지 않는 URL이 있습니다: {invalid[0]}",
            )
        )
        return

    options.output_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": not options.allow_playlist,
        "skip_download": True,
    }

    total_urls = len(urls)
    saved_count = 0
    last_title = ""

    try:
        for url_idx, url in enumerate(urls, start=1):
            if cancel_event.is_set():
                emit(ProgressEvent(kind="cancelled", message="작업이 취소되었습니다."))
                return

            platform = detect_platform(url)
            prefix = f"[{url_idx}/{total_urls}] " if total_urls > 1 else ""
            emit(
                ProgressEvent(
                    kind="status",
                    message=f"{prefix}{platform} 썸네일 정보 분석 중...",
                    percent=(url_idx - 0.8) / total_urls,
                )
            )
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if not info:
                continue

            entries = list(info["entries"]) if "entries" in info and info["entries"] else [info]
            entries = [e for e in entries if e]
            total_entries = len(entries)

            for idx, entry in enumerate(entries, start=1):
                if cancel_event.is_set():
                    emit(ProgressEvent(kind="cancelled", message="작업이 취소되었습니다."))
                    return

                title = str(entry.get("title") or f"thumbnail_{idx}")
                last_title = title
                safe_title = _sanitize_filename(title)
                candidates = _pick_thumbnail_candidates(entry, platform, options.quality)

                emit(
                    ProgressEvent(
                        kind="progress",
                        message=f"{prefix}썸네일 다운로드 중... ({idx}/{total_entries})",
                        percent=((url_idx - 1) + (idx / max(total_entries, 1))) / total_urls,
                        title=title,
                    )
                )

                raw_bytes: Optional[bytes] = None
                for cand_url in candidates:
                    if cancel_event.is_set():
                        emit(ProgressEvent(kind="cancelled", message="작업이 취소되었습니다."))
                        return
                    raw_bytes = _download_image_bytes(cand_url)
                    if raw_bytes:
                        break

                if not raw_bytes:
                    continue

                with Image.open(io.BytesIO(raw_bytes)) as img:
                    rgb_img = img.convert("RGB")
                    width, height = rgb_img.size
                    out_path = _unique_path(
                        options.output_dir / f"{safe_title}({width}x{height}).jpg"
                    )
                    rgb_img.save(out_path, format="JPEG", quality=95)
                    saved_count += 1

        if saved_count == 0:
            emit(
                ProgressEvent(
                    kind="error",
                    message="선택한 화질의 썸네일을 찾을 수 없습니다.",
                )
            )
            return

        emit(
            ProgressEvent(
                kind="done",
                message=f"썸네일 {saved_count}개 저장 완료!",
                percent=1.0,
                title=last_title,
            )
        )
    except yt_dlp.utils.DownloadError as exc:
        emit(ProgressEvent(kind="error", message=f"추출 오류: {exc}"))
    except Exception as exc:
        emit(ProgressEvent(kind="error", message=f"예기치 않은 오류: {exc}"))


@dataclass(frozen=True)
class LocalCutOptions:
    """Configuration for trimming an existing local audio file."""

    source_file: Path
    output_dir: Path
    start_time: str
    end_time: str
    max_duration: Optional[float] = None


def cut_local_audio(
    options: LocalCutOptions,
    emit: Callable[[ProgressEvent], None],
    cancel_event: threading.Event,
) -> None:
    """Trim an existing local audio file with millisecond precision."""
    src = options.source_file
    if not src.is_file():
        emit(ProgressEvent(kind="error", message="존재하는 오디오 파일을 선택해 주세요."))
        return

    ffmpeg_dir = find_ffmpeg_dir()
    if ffmpeg_dir is None:
        emit(
            ProgressEvent(
                kind="error",
                message="FFmpeg을 찾을 수 없습니다. bin 폴더 또는 시스템 PATH를 확인해 주세요.",
            )
        )
        return

    try:
        start_sec = parse_time_to_seconds(options.start_time)
        end_sec = parse_time_to_seconds(options.end_time)
    except ValueError as exc:
        emit(ProgressEvent(kind="error", message=str(exc)))
        return

    if start_sec is None and end_sec is None:
        emit(ProgressEvent(kind="error", message="시작 또는 종료 시간을 입력해 주세요."))
        return

    duration = options.max_duration if options.max_duration is not None else get_audio_duration(src)
    if duration is not None:
        if start_sec is not None and start_sec >= duration:
            emit(
                ProgressEvent(
                    kind="error",
                    message=f"시작 시간이 전체 길이({format_seconds_ms(duration)})를 초과했습니다.",
                )
            )
            return
        if end_sec is not None and end_sec > duration + 0.001:
            emit(
                ProgressEvent(
                    kind="error",
                    message=f"종료 시간이 전체 길이({format_seconds_ms(duration)})를 초과했습니다.",
                )
            )
            return

    if start_sec is not None and end_sec is not None and start_sec >= end_sec:
        emit(ProgressEvent(kind="error", message="종료 시간은 시작 시간보다 커야 합니다."))
        return

    if cancel_event.is_set():
        emit(ProgressEvent(kind="cancelled", message="작업이 취소되었습니다."))
        return

    options.output_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = _sanitize_filename(src.stem)
    dest = _unique_path(options.output_dir / f"{safe_stem}(cut){src.suffix}")

    emit(
        ProgressEvent(
            kind="status",
            message="오디오 구간 자르는 중...",
            percent=0.4,
            title=src.name,
        )
    )

    try:
        _run_ffmpeg_trim(ffmpeg_dir, src, dest, start_sec, end_sec)
        emit(
            ProgressEvent(
                kind="done",
                message=f"오디오 자르기 완료: {dest.name}",
                percent=1.0,
                title=dest.name,
            )
        )
    except Exception as exc:
        emit(ProgressEvent(kind="error", message=f"자르기 실패: {exc}"))
