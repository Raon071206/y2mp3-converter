"""CustomTkinter GUI for YouTube & SoundCloud Audio / Thumbnail extraction."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from tkinter import filedialog
from typing import Iterator, Optional

import customtkinter as ctk

from converter import (
    AudioExtractor,
    AudioTrimmer,
    ConversionOptions,
    LOSSLESS_FORMATS,
    LocalCutOptions,
    ProgressEvent,
    THUMBNAIL_SUPPORTED_FORMATS,
    ThumbnailExtractor,
    ThumbnailOptions,
    find_ffmpeg_dir,
    format_seconds_ms,
    get_app_dir,
    get_audio_duration,
    get_resource_dir,
    parse_time_to_seconds,
    parse_url_lines,
)


CONFIG_PATH = get_app_dir() / "config.json"

FORMAT_MAP = {
    "MP3": "mp3",
    "M4A": "m4a",
    "OGG": "ogg",
    "WAV (무손실)": "wav",
    "FLAC (무손실)": "flac",
}

BITRATE_MAP = {
    "320 kbps": "320",
    "256 kbps": "256",
    "192 kbps": "192",
    "128 kbps": "128",
}

THUMB_QUALITY_MAP = {
    "최고 화질 (자동)": "best",
    "1280x720 (maxresdefault)": "maxresdefault",
    "640x480 (sddefault)": "sddefault",
    "480x360 (hqdefault)": "hqdefault",
    "320x180 (mqdefault)": "mqdefault",
}


class Y2MP3App(ctk.CTk):
    """Main application window with separate Audio and Thumbnail extraction views."""

    def __init__(self) -> None:
        super().__init__()

        saved = self._load_config()
        ctk.set_appearance_mode(saved.get("appearance_mode", "System"))
        ctk.set_default_color_theme("blue")

        self.title("YouTube / SoundCloud 추출기")
        self.geometry("700x640")
        self.minsize(640, 600)

        icon_path = get_resource_dir() / "assets" / "app_icon.ico"
        if icon_path.is_file():
            self.iconbitmap(str(icon_path))

        self._event_queue: queue.Queue[ProgressEvent] = queue.Queue()
        self._cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._polling = False

        default_output = Path(saved.get("output_dir") or (Path.home() / "Downloads"))
        self._mode_var = ctk.StringVar(value="오디오 추출")
        self._output_dir_var = ctk.StringVar(value=str(default_output))

        # Audio mode options
        self._format_var = ctk.StringVar(value="MP3")
        self._bitrate_var = ctk.StringVar(value="192 kbps")
        self._metadata_var = ctk.BooleanVar(value=True)
        self._embed_thumb_var = ctk.BooleanVar(value=False)
        self._mp3_playlist_var = ctk.BooleanVar(value=False)
        self._start_time_var = ctk.StringVar()
        self._end_time_var = ctk.StringVar()

        # Thumbnail mode options
        self._thumb_quality_var = ctk.StringVar(value="최고 화질 (자동)")
        self._thumb_playlist_var = ctk.BooleanVar(value=False)

        # Local file cut mode options
        self._cut_file_var = ctk.StringVar()
        self._cut_start_var = ctk.StringVar(value="00:00:000")
        self._cut_end_var = ctk.StringVar()
        self._cut_max_duration: Optional[float] = None
        self._cut_start_var.trace_add("write", lambda *_: self._validate_cut_range())
        self._cut_end_var.trace_add("write", lambda *_: self._validate_cut_range())

        self._build_ui()
        self._update_ffmpeg_badge()
        self._on_format_change("MP3")
        self._on_mode_change("오디오 추출")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        # Top header + mode switcher
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=20, pady=(16, 6), sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            header,
            text="미디어 추출기",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        self._mode_switcher = ctk.CTkSegmentedButton(
            header,
            values=["오디오 추출", "썸네일 추출", "오디오 자르기"],
            variable=self._mode_var,
            command=self._on_mode_change,
            font=ctk.CTkFont(size=13, weight="bold"),
            height=34,
        )
        self._mode_switcher.grid(row=0, column=1, padx=14)

        self._ffmpeg_badge = ctk.CTkLabel(
            header,
            text="FFmpeg 확인 중...",
            font=ctk.CTkFont(size=12),
            corner_radius=6,
            padx=10,
            pady=3,
        )
        self._ffmpeg_badge.grid(row=0, column=2, padx=(0, 8), sticky="e")

        initial_theme = ctk.get_appearance_mode()
        self._theme_btn = ctk.CTkButton(
            header,
            text="라이트 모드" if initial_theme == "Dark" else "다크 모드",
            width=82,
            height=28,
            font=ctk.CTkFont(size=12),
            fg_color="transparent",
            border_width=1,
            text_color=("gray20", "gray85"),
            command=self._toggle_theme,
        )
        self._theme_btn.grid(row=0, column=3, sticky="e")

        # Shared multi-URL input card
        self._url_card = ctk.CTkFrame(self)
        self._url_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self._url_card,
            text="유튜브 / 사운드클라우드 URL (줄바꿈으로 여러 개 일괄 입력 가능)",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, padx=14, pady=(10, 4), sticky="w")

        self._url_textbox = ctk.CTkTextbox(self._url_card, height=72, wrap="none")
        self._url_textbox.grid(row=1, column=0, padx=(14, 8), pady=(0, 12), sticky="ew")

        url_btns = ctk.CTkFrame(self._url_card, fg_color="transparent")
        url_btns.grid(row=1, column=1, padx=(0, 14), pady=(0, 12), sticky="ns")

        ctk.CTkButton(
            url_btns,
            text="붙여넣기",
            width=76,
            height=32,
            command=self._paste_url,
        ).pack(pady=(0, 6))

        ctk.CTkButton(
            url_btns,
            text="지우기",
            width=76,
            height=32,
            fg_color="transparent",
            border_width=1,
            text_color=("gray20", "gray85"),
            command=lambda: self._url_textbox.delete("1.0", "end"),
        ).pack()

        # Local file picker card (for "파일 자르기" mode)
        self._local_file_card = ctk.CTkFrame(self)
        self._local_file_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self._local_file_card,
            text="자를 오디오 파일 선택 (MP3, M4A, WAV, FLAC)",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, padx=14, pady=(10, 4), sticky="w")

        self._cut_file_entry = ctk.CTkEntry(
            self._local_file_card,
            textvariable=self._cut_file_var,
            placeholder_text="로컬 오디오 파일을 선택하세요...",
            height=36,
        )
        self._cut_file_entry.grid(row=1, column=0, padx=(14, 8), pady=(0, 14), sticky="ew")

        ctk.CTkButton(
            self._local_file_card,
            text="파일 선택",
            width=84,
            height=36,
            command=self._browse_audio_file,
        ).grid(row=1, column=1, padx=(0, 14), pady=(0, 14))

        # Shared output folder card
        dir_card = ctk.CTkFrame(self)
        dir_card.grid(row=2, column=0, padx=20, pady=6, sticky="ew")
        dir_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            dir_card,
            text="저장 위치 (중복 파일 시 자동 번호 부여)",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, columnspan=3, padx=14, pady=(10, 4), sticky="w")

        self._dir_entry = ctk.CTkEntry(
            dir_card,
            textvariable=self._output_dir_var,
            height=34,
        )
        self._dir_entry.grid(row=1, column=0, padx=(14, 6), pady=(0, 12), sticky="ew")

        ctk.CTkButton(
            dir_card,
            text="폴더 선택",
            width=84,
            height=34,
            command=self._browse_folder,
        ).grid(row=1, column=1, padx=4, pady=(0, 12))

        ctk.CTkButton(
            dir_card,
            text="폴더 열기",
            width=84,
            height=34,
            fg_color="transparent",
            border_width=1,
            text_color=("gray20", "gray85"),
            command=self._open_output_folder,
        ).grid(row=1, column=2, padx=(4, 14), pady=(0, 12))

        # Mode 1: Audio options card
        self._audio_opts_frame = ctk.CTkFrame(self)
        self._audio_opts_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self._audio_opts_frame,
            text="오디오 추출 옵션 (저장 이름: 제목(음질).확장자)",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=14, pady=(10, 6), sticky="w")

        audio_row1 = ctk.CTkFrame(self._audio_opts_frame, fg_color="transparent")
        audio_row1.grid(row=1, column=0, padx=14, pady=(0, 8), sticky="ew")

        ctk.CTkLabel(audio_row1, text="포맷:").pack(side="left", padx=(0, 6))
        self._format_menu = ctk.CTkOptionMenu(
            audio_row1,
            variable=self._format_var,
            values=list(FORMAT_MAP.keys()),
            width=125,
            command=self._on_format_change,
        )
        self._format_menu.pack(side="left", padx=(0, 14))

        ctk.CTkLabel(audio_row1, text="음질:").pack(side="left", padx=(0, 6))
        self._bitrate_menu = ctk.CTkOptionMenu(
            audio_row1,
            variable=self._bitrate_var,
            values=list(BITRATE_MAP.keys()),
            width=105,
        )
        self._bitrate_menu.pack(side="left", padx=(0, 14))

        ctk.CTkCheckBox(
            audio_row1,
            text="메타데이터",
            variable=self._metadata_var,
            width=80,
        ).pack(side="left", padx=(0, 10))

        self._embed_thumb_chk = ctk.CTkCheckBox(
            audio_row1,
            text="앨범 아트",
            variable=self._embed_thumb_var,
            width=80,
        )
        self._embed_thumb_chk.pack(side="left", padx=(0, 10))

        ctk.CTkCheckBox(
            audio_row1,
            text="재생목록",
            variable=self._mp3_playlist_var,
            width=75,
        ).pack(side="left")

        audio_row2 = ctk.CTkFrame(self._audio_opts_frame, fg_color="transparent")
        audio_row2.grid(row=2, column=0, padx=14, pady=(0, 12), sticky="ew")

        ctk.CTkLabel(audio_row2, text="구간 자르기 (선택):").pack(side="left", padx=(0, 8))
        ctk.CTkEntry(
            audio_row2,
            textvariable=self._start_time_var,
            placeholder_text="시작 (예: 00:30)",
            width=120,
            height=30,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(audio_row2, text="~").pack(side="left", padx=(0, 6))
        ctk.CTkEntry(
            audio_row2,
            textvariable=self._end_time_var,
            placeholder_text="종료 (예: 03:45)",
            width=120,
            height=30,
        ).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(
            audio_row2,
            text="(비워두면 전체 추출)",
            font=ctk.CTkFont(size=11),
            text_color=("gray45", "gray60"),
        ).pack(side="left")

        # Mode 2: Thumbnail options card
        self._thumb_opts_frame = ctk.CTkFrame(self)
        self._thumb_opts_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self._thumb_opts_frame,
            text="썸네일 추출 옵션 (저장 이름: 제목(가로x세로).jpg)",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=14, pady=(10, 6), sticky="w")

        thumb_row = ctk.CTkFrame(self._thumb_opts_frame, fg_color="transparent")
        thumb_row.grid(row=1, column=0, padx=14, pady=(0, 12), sticky="ew")

        ctk.CTkLabel(thumb_row, text="화질:").pack(side="left", padx=(0, 8))
        ctk.CTkOptionMenu(
            thumb_row,
            variable=self._thumb_quality_var,
            values=list(THUMB_QUALITY_MAP.keys()),
            width=210,
        ).pack(side="left", padx=(0, 18))

        ctk.CTkCheckBox(
            thumb_row,
            text="재생목록 전체",
            variable=self._thumb_playlist_var,
        ).pack(side="left")

        # Mode 3: Local file cut options card
        self._cut_opts_frame = ctk.CTkFrame(self)
        self._cut_opts_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self._cut_opts_frame,
            text="오디오 자르기 옵션 (저장 이름: 원본이름(cut).확장자)",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=14, pady=(10, 6), sticky="w")

        cut_row = ctk.CTkFrame(self._cut_opts_frame, fg_color="transparent")
        cut_row.grid(row=1, column=0, padx=14, pady=(0, 4), sticky="ew")

        ctk.CTkLabel(cut_row, text="구간 (00:00:000):").pack(side="left", padx=(0, 8))
        ctk.CTkEntry(
            cut_row,
            textvariable=self._cut_start_var,
            placeholder_text="00:00:000",
            width=115,
            height=32,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(cut_row, text="~").pack(side="left", padx=(0, 6))
        ctk.CTkEntry(
            cut_row,
            textvariable=self._cut_end_var,
            placeholder_text="00:00:000",
            width=115,
            height=32,
        ).pack(side="left", padx=(0, 12))

        self._cut_duration_label = ctk.CTkLabel(
            self._cut_opts_frame,
            text="최대 길이: 파일을 선택하면 자동 감지됩니다",
            font=ctk.CTkFont(size=11),
            text_color=("gray45", "gray60"),
            anchor="w",
        )
        self._cut_duration_label.grid(row=2, column=0, padx=14, pady=(0, 10), sticky="ew")

        # Progress card
        prog_card = ctk.CTkFrame(self)
        prog_card.grid(row=4, column=0, padx=20, pady=6, sticky="ew")
        prog_card.grid_columnconfigure(0, weight=1)

        self._title_label = ctk.CTkLabel(
            prog_card,
            text="대기 중",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        )
        self._title_label.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")

        self._progress_bar = ctk.CTkProgressBar(prog_card, height=12)
        self._progress_bar.set(0)
        self._progress_bar.grid(row=1, column=0, padx=14, pady=4, sticky="ew")

        self._status_label = ctk.CTkLabel(
            prog_card,
            text="유튜브 또는 사운드클라우드 링크를 입력하고 작업을 시작하세요.",
            font=ctk.CTkFont(size=12),
            text_color=("gray35", "gray70"),
            anchor="w",
        )
        self._status_label.grid(row=2, column=0, padx=14, pady=(2, 10), sticky="ew")

        # Action buttons
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=5, column=0, padx=20, pady=(10, 18), sticky="sew")
        btn_row.grid_columnconfigure(0, weight=1)

        self._start_btn = ctk.CTkButton(
            btn_row,
            text="오디오 변환 시작",
            height=42,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._start_job,
        )
        self._start_btn.grid(row=0, column=0, padx=(0, 8), sticky="ew")

        self._cancel_btn = ctk.CTkButton(
            btn_row,
            text="취소",
            width=100,
            height=42,
            fg_color="#B91C1C",
            hover_color="#991B1B",
            state="disabled",
            command=self._cancel_job,
        )
        self._cancel_btn.grid(row=0, column=1, sticky="e")

    def _on_format_change(self, label: str) -> None:
        fmt = FORMAT_MAP.get(label, "mp3")
        is_lossless = fmt in LOSSLESS_FORMATS
        self._bitrate_menu.configure(state="disabled" if is_lossless else "normal")
        self._embed_thumb_chk.configure(
            state="normal" if fmt in THUMBNAIL_SUPPORTED_FORMATS else "disabled"
        )

    def _on_mode_change(self, mode: str) -> None:
        self._thumb_opts_frame.grid_forget()
        self._audio_opts_frame.grid_forget()
        self._cut_opts_frame.grid_forget()

        if mode == "오디오 추출":
            self._local_file_card.grid_forget()
            self._url_card.grid(row=1, column=0, padx=20, pady=6, sticky="ew")
            self._audio_opts_frame.grid(row=3, column=0, padx=20, pady=6, sticky="ew")
            self._start_btn.configure(text="오디오 변환 시작", state="normal")
        elif mode == "썸네일 추출":
            self._local_file_card.grid_forget()
            self._url_card.grid(row=1, column=0, padx=20, pady=6, sticky="ew")
            self._thumb_opts_frame.grid(row=3, column=0, padx=20, pady=6, sticky="ew")
            self._start_btn.configure(text="썸네일 추출 시작", state="normal")
        else:
            self._url_card.grid_forget()
            self._local_file_card.grid(row=1, column=0, padx=20, pady=6, sticky="ew")
            self._cut_opts_frame.grid(row=3, column=0, padx=20, pady=6, sticky="ew")
            self._start_btn.configure(text="오디오 자르기 시작")
            self._validate_cut_range()

    @staticmethod
    def _load_config() -> dict:
        if not CONFIG_PATH.is_file():
            return {}
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_config(self) -> None:
        data = {
            "appearance_mode": ctk.get_appearance_mode(),
            "output_dir": self._output_dir_var.get().strip()
            or str(Path.home() / "Downloads"),
        }
        try:
            CONFIG_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _on_close(self) -> None:
        self._save_config()
        self.destroy()

    @contextmanager
    def _freeze_redraw(self) -> Iterator[None]:
        """Suspend Win32 window painting during bulk widget theme updates to prevent flicker."""
        if sys.platform != "win32":
            yield
            return

        user32 = ctypes.windll.user32
        WM_SETREDRAW = 0x000B
        GA_ROOT = 2
        RDW_ERASE = 0x0004
        RDW_INVALIDATE = 0x0001
        RDW_ALLCHILDREN = 0x0080
        RDW_UPDATENOW = 0x0100

        hwnd = user32.GetAncestor(self.winfo_id(), GA_ROOT) or self.winfo_id()
        user32.SendMessageW(hwnd, WM_SETREDRAW, 0, 0)
        try:
            yield
            self.update_idletasks()
        finally:
            user32.SendMessageW(hwnd, WM_SETREDRAW, 1, 0)
            user32.RedrawWindow(
                hwnd,
                None,
                None,
                RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW,
            )

    def _toggle_theme(self) -> None:
        current = ctk.get_appearance_mode()
        next_mode = "Light" if current == "Dark" else "Dark"
        next_label = "다크 모드" if next_mode == "Light" else "라이트 모드"

        self._theme_btn.configure(state="disabled")
        with self._freeze_redraw():
            ctk.set_appearance_mode(next_mode)
            self._theme_btn.configure(text=next_label)
        self._save_config()
        self.after(80, lambda: self._theme_btn.configure(state="normal"))

    def _update_ffmpeg_badge(self) -> None:
        if find_ffmpeg_dir() is not None:
            self._ffmpeg_badge.configure(
                text="FFmpeg 준비됨",
                fg_color=("#DCFCE7", "#14532D"),
                text_color=("#166534", "#BBF7D0"),
            )
        else:
            self._ffmpeg_badge.configure(
                text="FFmpeg 없음",
                fg_color=("#FEE2E2", "#7F1D1D"),
                text_color=("#991B1B", "#FECACA"),
            )

    def _paste_url(self) -> None:
        try:
            text = self.clipboard_get().strip()
            if not text:
                return
            current = self._url_textbox.get("1.0", "end").strip()
            if current:
                self._url_textbox.insert("end", f"\n{text}")
            else:
                self._url_textbox.insert("1.0", text)
        except Exception:
            pass

    def _browse_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self._output_dir_var.get())
        if selected:
            self._output_dir_var.set(selected)
            self._save_config()

    def _browse_audio_file(self) -> None:
        selected = filedialog.askopenfilename(
            initialdir=self._output_dir_var.get(),
            filetypes=[
                ("오디오 파일", "*.mp3 *.m4a *.wav *.flac *.ogg *.aac"),
                ("모든 파일", "*.*"),
            ],
        )
        if not selected:
            return
        self._cut_file_var.set(selected)
        duration = get_audio_duration(Path(selected))
        self._cut_max_duration = duration
        if duration is not None:
            formatted = format_seconds_ms(duration)
            self._cut_end_var.set(formatted)
            self._cut_duration_label.configure(
                text=f"최대 길이: {formatted}",
                text_color=("gray45", "gray60"),
            )
        else:
            self._cut_duration_label.configure(
                text="최대 길이를 감지할 수 없습니다",
                text_color=("gray45", "gray60"),
            )
        self._validate_cut_range()

    def _validate_cut_range(self) -> None:
        if self._mode_var.get() != "오디오 자르기":
            return
        max_dur = self._cut_max_duration
        if max_dur is None:
            self._start_btn.configure(state="normal")
            return

        max_text = format_seconds_ms(max_dur)
        try:
            start_sec = parse_time_to_seconds(self._cut_start_var.get())
            end_sec = parse_time_to_seconds(self._cut_end_var.get())
        except ValueError:
            self._cut_duration_label.configure(
                text=f"최대 길이: {max_text} · 시간 형식 오류 (예: 00:00:000)",
                text_color=("#DC2626", "#F87171"),
            )
            self._start_btn.configure(state="disabled")
            return

        if (start_sec is not None and start_sec >= max_dur) or (
            end_sec is not None and end_sec > max_dur + 0.001
        ):
            self._cut_duration_label.configure(
                text=f"최대 길이({max_text})를 초과할 수 없습니다",
                text_color=("#DC2626", "#F87171"),
            )
            self._start_btn.configure(state="disabled")
            return

        if start_sec is not None and end_sec is not None and start_sec >= end_sec:
            self._cut_duration_label.configure(
                text=f"최대 길이: {max_text} · 종료 시간은 시작 시간보다 커야 합니다",
                text_color=("#DC2626", "#F87171"),
            )
            self._start_btn.configure(state="disabled")
            return

        self._cut_duration_label.configure(
            text=f"최대 길이: {max_text}",
            text_color=("gray45", "gray60"),
        )
        self._start_btn.configure(state="normal")

    def _open_output_folder(self) -> None:
        target = Path(self._output_dir_var.get().strip() or str(Path.home() / "Downloads"))
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target))

    def _set_running_state(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self._start_btn.configure(state=state)
        self._cancel_btn.configure(state="normal" if running else "disabled")
        self._url_textbox.configure(state=state)
        self._mode_switcher.configure(state=state)

    def _start_job(self) -> None:
        if self._worker and self._worker.is_alive():
            return

        urls = parse_url_lines(self._url_textbox.get("1.0", "end"))
        output_dir = Path(self._output_dir_var.get().strip() or str(Path.home() / "Downloads"))

        self._cancel_event.clear()
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except queue.Empty:
                break

        self._progress_bar.set(0)
        self._title_label.configure(text="준비 중...")
        self._status_label.configure(text="작업을 시작합니다...")
        self._set_running_state(True)

        mode = self._mode_var.get()
        if mode == "오디오 추출":
            options = ConversionOptions(
                urls=urls,
                output_dir=output_dir,
                audio_format=FORMAT_MAP.get(self._format_var.get(), "mp3"),
                bitrate=BITRATE_MAP.get(self._bitrate_var.get(), "192"),
                allow_playlist=self._mp3_playlist_var.get(),
                embed_metadata=self._metadata_var.get(),
                embed_thumbnail=self._embed_thumb_var.get(),
                start_time=self._start_time_var.get(),
                end_time=self._end_time_var.get(),
            )
            processor = AudioExtractor(self._event_queue.put, self._cancel_event)
        elif mode == "썸네일 추출":
            options = ThumbnailOptions(
                urls=urls,
                output_dir=output_dir,
                quality=THUMB_QUALITY_MAP.get(self._thumb_quality_var.get(), "best"),
                allow_playlist=self._thumb_playlist_var.get(),
            )
            processor = ThumbnailExtractor(self._event_queue.put, self._cancel_event)
        else:
            options = LocalCutOptions(
                source_file=Path(self._cut_file_var.get().strip()),
                output_dir=output_dir,
                start_time=self._cut_start_var.get(),
                end_time=self._cut_end_var.get(),
                max_duration=self._cut_max_duration,
            )
            processor = AudioTrimmer(self._event_queue.put, self._cancel_event)

        self._worker = threading.Thread(
            target=processor.run,
            args=(options,),
            daemon=True,
        )
        self._worker.start()

        if not self._polling:
            self._polling = True
            self.after(80, self._poll_events)

    def _cancel_job(self) -> None:
        self._cancel_event.set()
        self._status_label.configure(text="취소 요청 중...")
        self._cancel_btn.configure(state="disabled")

    def _poll_events(self) -> None:
        finished = False
        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break

            if event.title:
                display_title = (
                    event.title if len(event.title) <= 55 else event.title[:52] + "..."
                )
                self._title_label.configure(text=display_title)

            if event.kind in ("status", "progress"):
                self._progress_bar.set(event.percent)
                details = [event.message]
                if event.speed_text:
                    details.append(event.speed_text)
                if event.eta_text:
                    details.append(event.eta_text)
                self._status_label.configure(text=" · ".join(details))
            elif event.kind == "done":
                self._progress_bar.set(1.0)
                self._status_label.configure(text=event.message)
                finished = True
            elif event.kind in ("error", "cancelled"):
                self._progress_bar.set(0)
                self._status_label.configure(text=event.message)
                finished = True

        if finished:
            self._polling = False
            self._set_running_state(False)
        else:
            self.after(80, self._poll_events)


def main() -> None:
    app = Y2MP3App()
    app.mainloop()


if __name__ == "__main__":
    main()
