import asyncio
import contextlib
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import astrbot.core.message.components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_liars_bar_basic"

COMMAND_PREFIXES = ["/酒馆", "酒馆", "/骗子酒馆", "骗子酒馆"]

CARD_SUN = "sun"
CARD_MOON = "moon"
CARD_STAR = "star"
CARD_MAGIC = "magic"
TARGET_CARDS = [CARD_SUN, CARD_MOON, CARD_STAR]
CARD_NAME = {
    CARD_SUN: "太阳",
    CARD_MOON: "月亮",
    CARD_STAR: "星星",
    CARD_MAGIC: "魔术",
}

WIRE_RED = "红"
WIRE_BLUE = "蓝"
WIRE_YELLOW = "黄"
WIRE_COLORS = [WIRE_RED, WIRE_BLUE, WIRE_YELLOW]
WIRE_ALIASES = {
    "红": WIRE_RED,
    "红线": WIRE_RED,
    "red": WIRE_RED,
    "r": WIRE_RED,
    "蓝": WIRE_BLUE,
    "蓝线": WIRE_BLUE,
    "blue": WIRE_BLUE,
    "b": WIRE_BLUE,
    "黄": WIRE_YELLOW,
    "黄线": WIRE_YELLOW,
    "yellow": WIRE_YELLOW,
    "y": WIRE_YELLOW,
}

DEFAULT_PLAY_TIMEOUT_SECONDS = 120
DEFAULT_WIRE_TIMEOUT_SECONDS = 120
FIXED_HAND_SIZE = 5
DECK_DISTRIBUTION_WEIGHTS = {
    CARD_SUN: 3,
    CARD_MOON: 3,
    CARD_STAR: 3,
    CARD_MAGIC: 1,
}

PHASE_WAITING = "waiting"
PHASE_PLAYING = "playing"
PHASE_AWAIT_WIRE = "await_wire"

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False


@dataclass
class PlayerState:
    user_id: str
    name: str
    alive: bool = True
    hand: list[str] = field(default_factory=list)
    bomb_color: str = ""
    wires_remaining: list[str] = field(default_factory=lambda: WIRE_COLORS.copy())
    dm_reachable: bool = False

    def reset_for_new_game(self) -> None:
        self.alive = True
        self.hand = []
        self.bomb_color = random.choice(WIRE_COLORS)
        self.wires_remaining = WIRE_COLORS.copy()
        self.dm_reachable = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "alive": self.alive,
            "hand": self.hand,
            "bomb_color": self.bomb_color,
            "wires_remaining": self.wires_remaining,
            "dm_reachable": self.dm_reachable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlayerState":
        return cls(
            user_id=str(data.get("user_id", "")),
            name=str(data.get("name", "")),
            alive=bool(data.get("alive", True)),
            hand=list(data.get("hand", []) or []),
            bomb_color=str(data.get("bomb_color", "")),
            wires_remaining=list(data.get("wires_remaining", WIRE_COLORS.copy()) or []),
            dm_reachable=bool(data.get("dm_reachable", False)),
        )


@dataclass
class LastPlay:
    player_id: str
    cards: list[str]
    declared_target: str
    played_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "cards": self.cards,
            "declared_target": self.declared_target,
            "played_at": self.played_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LastPlay":
        return cls(
            player_id=str(data.get("player_id", "")),
            cards=list(data.get("cards", []) or []),
            declared_target=str(data.get("declared_target", "")),
            played_at=float(data.get("played_at", 0.0)),
        )


@dataclass
class RoomState:
    room_id: str
    group_umo: str
    group_id: str
    platform_id: str
    bot_id: str
    owner_id: str
    owner_name: str
    phase: str = PHASE_WAITING
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float = 0.0
    round_no: int = 0
    dealer_cursor: int = 0
    players: dict[str, PlayerState] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    target_card: str = ""
    current_turn_user_id: str = ""
    last_play: Optional[LastPlay] = None
    pending_wire_user_id: str = ""
    wire_options: list[str] = field(default_factory=list)
    wire_index_map: dict[str, str] = field(default_factory=dict)
    initial_player_count: int = 0
    fixed_hand_size: int = FIXED_HAND_SIZE
    round_deck_total: int = 0
    round_deck_counts: dict[str, int] = field(default_factory=dict)
    play_deadline_ts: float = 0.0
    wire_deadline_ts: float = 0.0
    action_token: int = 0

    def alive_ids(self) -> list[str]:
        return [uid for uid in self.order if uid in self.players and self.players[uid].alive]

    def to_dict(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "group_umo": self.group_umo,
            "group_id": self.group_id,
            "platform_id": self.platform_id,
            "bot_id": self.bot_id,
            "owner_id": self.owner_id,
            "owner_name": self.owner_name,
            "phase": self.phase,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "round_no": self.round_no,
            "dealer_cursor": self.dealer_cursor,
            "players": {uid: p.to_dict() for uid, p in self.players.items()},
            "order": self.order,
            "target_card": self.target_card,
            "current_turn_user_id": self.current_turn_user_id,
            "last_play": self.last_play.to_dict() if self.last_play else None,
            "pending_wire_user_id": self.pending_wire_user_id,
            "wire_options": self.wire_options,
            "wire_index_map": self.wire_index_map,
            "initial_player_count": self.initial_player_count,
            "fixed_hand_size": self.fixed_hand_size,
            "round_deck_total": self.round_deck_total,
            "round_deck_counts": self.round_deck_counts,
            "play_deadline_ts": self.play_deadline_ts,
            "wire_deadline_ts": self.wire_deadline_ts,
            "action_token": self.action_token,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoomState":
        room = cls(
            room_id=str(data.get("room_id", "")),
            group_umo=str(data.get("group_umo", "")),
            group_id=str(data.get("group_id", "")),
            platform_id=str(data.get("platform_id", "")),
            bot_id=str(data.get("bot_id", "")),
            owner_id=str(data.get("owner_id", "")),
            owner_name=str(data.get("owner_name", "")),
            phase=str(data.get("phase", PHASE_WAITING)),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            started_at=float(data.get("started_at", 0.0)),
            round_no=int(data.get("round_no", 0)),
            dealer_cursor=int(data.get("dealer_cursor", 0)),
            order=list(data.get("order", []) or []),
            target_card=str(data.get("target_card", "")),
            current_turn_user_id=str(data.get("current_turn_user_id", "")),
            pending_wire_user_id=str(data.get("pending_wire_user_id", "")),
            wire_options=list(data.get("wire_options", []) or []),
            wire_index_map=dict(data.get("wire_index_map", {}) or {}),
            initial_player_count=int(data.get("initial_player_count", 0)),
            fixed_hand_size=int(data.get("fixed_hand_size", FIXED_HAND_SIZE)),
            round_deck_total=int(data.get("round_deck_total", 0)),
            round_deck_counts=dict(data.get("round_deck_counts", {}) or {}),
            play_deadline_ts=float(data.get("play_deadline_ts", 0.0)),
            wire_deadline_ts=float(data.get("wire_deadline_ts", 0.0)),
            action_token=int(data.get("action_token", 0)),
        )
        players_raw = data.get("players", {}) or {}
        room.players = {
            str(uid): PlayerState.from_dict(raw)
            for uid, raw in players_raw.items()
            if isinstance(raw, dict)
        }
        last_play_raw = data.get("last_play")
        if isinstance(last_play_raw, dict):
            room.last_play = LastPlay.from_dict(last_play_raw)
        return room


class AssetRenderer:
    def __init__(self, assets_dir: Path, cache_dir: Path):
        self.assets_dir = assets_dir
        self.cards_dir = assets_dir / "cards"
        self.bombs_dir = assets_dir / "bombs"
        self.cache_dir = cache_dir

    def ensure_assets(self) -> None:
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        self.bombs_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if not PIL_AVAILABLE:
            return

        self._ensure_card_assets()
        self._ensure_bomb_assets()

    def card_path(self, card: str) -> Path:
        return self.cards_dir / f"{card}.png"

    def target_path(self, target: str) -> Path:
        target_file = self.cards_dir / f"target_{target}.png"
        if target_file.exists():
            return target_file
        return self.card_path(target)

    def bomb_path_for_options(self, options: list[str]) -> Optional[Path]:
        ordered = [c for c in WIRE_COLORS if c in options]
        if len(ordered) == 3:
            return self.bombs_dir / "bomb_3_rby.png"
        if len(ordered) == 2:
            key = "".join(["r" if c == WIRE_RED else "b" if c == WIRE_BLUE else "y" for c in ordered])
            return self.bombs_dir / f"bomb_2_{key}.png"
        if len(ordered) == 1:
            key = "r" if ordered[0] == WIRE_RED else "b" if ordered[0] == WIRE_BLUE else "y"
            return self.bombs_dir / f"bomb_1_{key}.png"
        return None

    def bomb_explode_path(self) -> Path:
        return self.bombs_dir / "bomb_explode.png"

    def build_hand_image(
        self,
        room_id: str,
        user_id: str,
        cards: list[str],
        width_hint: int = 960,
    ) -> Optional[Path]:
        if not PIL_AVAILABLE:
            return None
        if not cards:
            return None

        card_imgs: list[Image.Image] = []
        for code in cards:
            path = self.card_path(code)
            if not path.exists():
                continue
            card_imgs.append(Image.open(path).convert("RGBA"))

        if not card_imgs:
            return None

        card_w = card_imgs[0].width
        card_h = card_imgs[0].height
        spacing = 18
        label_h = 38
        padding = 20

        cols = max(1, min(len(card_imgs), max(3, width_hint // (card_w + spacing))))
        rows = (len(card_imgs) + cols - 1) // cols

        canvas_w = padding * 2 + cols * card_w + (cols - 1) * spacing
        canvas_h = padding * 2 + rows * (card_h + label_h) + (rows - 1) * spacing
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (18, 20, 27, 255))

        draw = ImageDraw.Draw(canvas)
        title_font = self._pick_font(24)
        idx_font = self._pick_number_font(28)

        draw.text((padding, 4), "你的手牌（序号用于 /酒馆 出）", fill=(233, 237, 245, 255), font=title_font)

        for i, img in enumerate(card_imgs):
            row = i // cols
            col = i % cols
            x = padding + col * (card_w + spacing)
            y = padding + row * (card_h + label_h)
            canvas.alpha_composite(img, (x, y + label_h))
            idx_text = str(i + 1)
            tw = self._text_width(draw, idx_text, idx_font)
            draw.rounded_rectangle((x, y, x + max(36, tw + 20), y + 30), radius=10, fill=(45, 53, 75, 220))
            draw.text(
                (x + 10, y + 4),
                idx_text,
                fill=(255, 255, 255, 255),
                font=idx_font,
                stroke_width=1,
                stroke_fill=(0, 0, 0, 220),
            )

        safe_room_id = str(abs(hash(room_id)))
        out = self.cache_dir / f"hand_{safe_room_id}_{user_id}_{int(time.time() * 1000)}.png"
        canvas.convert("RGB").save(out, format="PNG")
        return out

    def _ensure_card_assets(self) -> None:
        specs = {
            CARD_SUN: ("太阳", (246, 162, 42), (255, 226, 128), "☀"),
            CARD_MOON: ("月亮", (81, 120, 235), (158, 196, 255), "☾"),
            CARD_STAR: ("星星", (133, 84, 216), (214, 170, 255), "★"),
            CARD_MAGIC: ("魔术", (39, 171, 136), (122, 238, 202), "✦"),
            "back": ("酒馆", (67, 49, 38), (139, 98, 67), "♣"),
        }
        for code, (title, c1, c2, symbol) in specs.items():
            path = self.cards_dir / f"{code}.png"
            if not path.exists():
                self._create_card(path, title, c1, c2, symbol)

        for target in TARGET_CARDS:
            target_path = self.target_path(target)
            if target_path.exists():
                continue
            src = self.card_path(target)
            if src.exists():
                img = Image.open(src).convert("RGB")
                img.save(target_path, format="PNG")

    def _ensure_bomb_assets(self) -> None:
        options_list = [
            [WIRE_RED, WIRE_BLUE, WIRE_YELLOW],
            [WIRE_RED, WIRE_BLUE],
            [WIRE_RED, WIRE_YELLOW],
            [WIRE_BLUE, WIRE_YELLOW],
            [WIRE_RED],
            [WIRE_BLUE],
            [WIRE_YELLOW],
        ]
        for options in options_list:
            out = self.bomb_path_for_options(options)
            if out and not out.exists():
                self._create_bomb(out, options)

        explode = self.bomb_explode_path()
        if not explode.exists():
            self._create_explode(explode)

    def _create_card(
        self,
        out_path: Path,
        title: str,
        color_a: tuple[int, int, int],
        color_b: tuple[int, int, int],
        symbol: str,
    ) -> None:
        w, h = 210, 300
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(base)

        for y in range(h):
            t = y / max(1, h - 1)
            r = int(color_a[0] * (1 - t) + color_b[0] * t)
            g = int(color_a[1] * (1 - t) + color_b[1] * t)
            b = int(color_a[2] * (1 - t) + color_b[2] * t)
            draw.line((0, y, w, y), fill=(r, g, b, 255))

        texture = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        tex = ImageDraw.Draw(texture)
        for _ in range(240):
            x1 = random.randint(0, w)
            y1 = random.randint(0, h)
            x2 = x1 + random.randint(-36, 36)
            y2 = y1 + random.randint(-36, 36)
            tex.line((x1, y1, x2, y2), fill=(255, 255, 255, random.randint(8, 28)), width=1)
        base = Image.alpha_composite(base, texture)

        mask = Image.new("L", (w, h), 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.rounded_rectangle((0, 0, w - 1, h - 1), radius=24, fill=255)
        img.paste(base, (0, 0), mask)

        draw = ImageDraw.Draw(img)
        title_font = self._pick_font(34)
        symbol_font = self._pick_font(120)
        small_font = self._pick_font(24)

        draw.rounded_rectangle((10, 10, w - 11, h - 11), radius=22, outline=(255, 255, 255, 200), width=3)
        draw.rounded_rectangle((18, 18, w - 19, 70), radius=14, fill=(12, 14, 21, 180))
        tw = self._text_width(draw, title, title_font)
        draw.text(((w - tw) // 2, 28), title, fill=(255, 255, 255, 255), font=title_font)

        sw = self._text_width(draw, symbol, symbol_font)
        draw.text(((w - sw) // 2, 98), symbol, fill=(255, 255, 255, 230), font=symbol_font)

        bottom = "LIAR'S BAR"
        bw = self._text_width(draw, bottom, small_font)
        draw.rounded_rectangle((26, h - 50, w - 27, h - 16), radius=12, fill=(15, 17, 22, 190))
        draw.text(((w - bw) // 2, h - 43), bottom, fill=(236, 236, 236, 255), font=small_font)

        img.convert("RGB").save(out_path, format="PNG")

    def _create_bomb(self, out_path: Path, options: list[str]) -> None:
        w, h = 640, 360
        bg = Image.new("RGBA", (w, h), (18, 18, 24, 255))
        draw = ImageDraw.Draw(bg)

        for y in range(h):
            c = int(26 + 40 * (y / h))
            draw.line((0, y, w, y), fill=(c, c, c + 6, 255))

        for _ in range(520):
            x = random.randint(0, w - 1)
            y = random.randint(0, h - 1)
            draw.point((x, y), fill=(255, 255, 255, random.randint(6, 18)))

        # Bomb body
        cx, cy, r = 320, 220, 112
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(46, 49, 56, 255), outline=(165, 170, 180, 255), width=6)
        draw.ellipse((cx - 50, cy - 50, cx + 50, cy + 50), fill=(20, 21, 25, 255), outline=(86, 90, 100, 255), width=4)

        info_font = self._pick_font(28)

        color_to_rgb = {
            WIRE_RED: (235, 71, 71),
            WIRE_BLUE: (69, 132, 255),
            WIRE_YELLOW: (241, 212, 83),
        }
        slot_x = [220, 320, 420]
        labels = [WIRE_RED, WIRE_BLUE, WIRE_YELLOW]
        for i, color in enumerate(labels):
            active = color in options
            x = slot_x[i]
            y1, y2 = 80, 170
            if active:
                rgb = color_to_rgb[color]
                draw.line((x, y1, x, y2), fill=rgb + (255,), width=12)
                draw.ellipse((x - 14, y1 - 14, x + 14, y1 + 14), fill=rgb + (255,))
            else:
                draw.line((x, y1, x, y2), fill=(72, 72, 77, 255), width=8)

        draw.rounded_rectangle((154, h - 64, w - 154, h - 20), radius=12, fill=(14, 16, 23, 220))
        footer = "剩余可剪线"
        fw = self._text_width(draw, footer, info_font)
        draw.text(((w - fw) // 2, h - 53), footer, fill=(236, 236, 240, 255), font=info_font)

        bg.convert("RGB").save(out_path, format="PNG")

    def _create_explode(self, out_path: Path) -> None:
        w, h = 640, 360
        img = Image.new("RGBA", (w, h), (22, 10, 8, 255))
        draw = ImageDraw.Draw(img)

        for y in range(h):
            t = y / max(1, h - 1)
            r = int(65 + 130 * t)
            g = int(16 + 24 * t)
            b = int(12 + 18 * t)
            draw.line((0, y, w, y), fill=(r, g, b, 255))

        for _ in range(260):
            x = random.randint(0, w - 1)
            y = random.randint(0, h - 1)
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=(255, random.randint(90, 180), 10, random.randint(120, 220)))

        center_font = self._pick_font(72)
        small_font = self._pick_font(34)
        t1 = "BOOM"
        w1 = self._text_width(draw, t1, center_font)
        draw.text(((w - w1) // 2, 120), t1, fill=(255, 236, 181, 255), font=center_font)
        t2 = "剪到爆线，玩家出局"
        w2 = self._text_width(draw, t2, small_font)
        draw.text(((w - w2) // 2, 218), t2, fill=(255, 244, 227, 255), font=small_font)

        img.convert("RGB").save(out_path, format="PNG")

    @staticmethod
    def _pick_font(size: int) -> ImageFont.ImageFont:
        candidates = [
            "/root/AstrBot/data/plugins/astrbot_plugin_gaokao_sim/assets/fonts/SourceHanSansSC-Regular.otf",
            "/root/AstrBot/data/plugins/astrbot_plugin_parser/core/resources/HYSongYunLangHeiW-1.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size=size)
                except Exception:
                    continue
        return ImageFont.load_default()

    @staticmethod
    def _pick_number_font(size: int) -> ImageFont.ImageFont:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size=size)
                except Exception:
                    continue
        return ImageFont.load_default()

    @staticmethod
    def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
        bbox = draw.textbbox((0, 0), text, font=font)
        return max(0, int(bbox[2] - bbox[0]))


@register(PLUGIN_NAME, "金幺", "骗子酒馆基础版（本地素材）", "0.1.0", "local")
class LiarsBarBasicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config

        self.plugin_dir = Path(__file__).resolve().parent
        self.assets_dir = self.plugin_dir / "assets"

        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"

        self.renderer = AssetRenderer(self.assets_dir, self.cache_dir)

        self.state_lock = asyncio.Lock()
        self.rooms: dict[str, RoomState] = {}
        self.player_room_index: dict[str, str] = {}
        self.recent_event_cache: dict[str, float] = {}

        self.play_timeout_tasks: dict[str, asyncio.Task] = {}
        self.wire_timeout_tasks: dict[str, asyncio.Task] = {}
        self.cleanup_task: Optional[asyncio.Task] = None

        self._load_state()

    async def initialize(self):
        self.renderer.ensure_assets()
        await self._resume_timers()
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def terminate(self):
        if self.cleanup_task:
            self.cleanup_task.cancel()
        for task in list(self.play_timeout_tasks.values()):
            task.cancel()
        for task in list(self.wire_timeout_tasks.values()):
            task.cancel()
        self.play_timeout_tasks.clear()
        self.wire_timeout_tasks.clear()

    def _play_timeout_seconds(self) -> int:
        return max(1, int(self.conf.get("play_timeout_seconds", DEFAULT_PLAY_TIMEOUT_SECONDS)))

    def _wire_timeout_seconds(self) -> int:
        return max(1, int(self.conf.get("wire_timeout_seconds", DEFAULT_WIRE_TIMEOUT_SECONDS)))

    def _build_locked_deck_counts(self, start_player_count: int) -> dict[str, int]:
        # Lock total cards at game start: players * 5.
        players = max(3, min(5, int(start_player_count)))
        total = players * FIXED_HAND_SIZE
        weights = DECK_DISTRIBUTION_WEIGHTS
        cards = [CARD_SUN, CARD_MOON, CARD_STAR, CARD_MAGIC]
        weight_sum = sum(weights.values())

        raw: dict[str, float] = {}
        counts: dict[str, int] = {}
        for card in cards:
            value = total * (weights[card] / weight_sum)
            raw[card] = value
            counts[card] = int(value)

        remain = total - sum(counts.values())
        order = sorted(cards, key=lambda c: (raw[c] - counts[c]), reverse=True)
        idx = 0
        while remain > 0:
            counts[order[idx % len(order)]] += 1
            idx += 1
            remain -= 1
        return counts

    def _build_round_deck(self, room: RoomState) -> list[str]:
        counts = room.round_deck_counts or self._build_locked_deck_counts(room.initial_player_count or len(room.order) or 4)
        deck = (
            [CARD_SUN] * int(counts.get(CARD_SUN, 0))
            + [CARD_MOON] * int(counts.get(CARD_MOON, 0))
            + [CARD_STAR] * int(counts.get(CARD_STAR, 0))
            + [CARD_MAGIC] * int(counts.get(CARD_MAGIC, 0))
        )
        random.shuffle(deck)
        return deck

    def _card_pool_text(self, room: RoomState) -> str:
        hand_size = max(1, int(room.fixed_hand_size or FIXED_HAND_SIZE))
        counts = room.round_deck_counts or self._build_locked_deck_counts(room.initial_player_count or len(room.order) or 4)
        total = int(room.round_deck_total or sum(int(v) for v in counts.values()))
        alive_n = len(room.alive_ids())
        used = hand_size * alive_n
        remain = max(0, total - used)
        return (
            f"本大局锁定卡池：共 {total} 张（太阳×{int(counts.get(CARD_SUN, 0))}  月亮×{int(counts.get(CARD_MOON, 0))}  星星×{int(counts.get(CARD_STAR, 0))}  魔术×{int(counts.get(CARD_MAGIC, 0))}）"
            f"\n开局人数 {room.initial_player_count}，当前存活 {alive_n} 人；每位存活玩家固定发 {hand_size} 张，本小局未发 {remain} 张。"
        )

    def _room_create_card_intro(self) -> str:
        return (
            "发牌规则：每小局先清空上局手牌，再给当前存活玩家每人固定发 5 张。"
            "\n卡池规则：在 /酒馆 开始 时按开局人数一次锁定总牌数（3人=15张，4人=20张，5人=25张），后续小局不再改变。"
            "\n牌型按比例分配（太阳/月亮/星星/魔术=3/3/3/1），保证每局结构稳定。"
        )

    @filter.command("酒馆", alias={"骗子酒馆", "liarsbar"})
    async def liars_bar(self, event: AstrMessageEvent):
        event.stop_event()
        if self._is_duplicate_event(event):
            return

        text = (event.message_str or "").strip()
        rest = self._strip_command_prefix(text)
        tokens = [tk for tk in rest.split() if tk]
        sub = tokens[0] if tokens else "帮助"
        args = tokens[1:]

        if event.is_private_chat():
            await self._handle_private_command(event, sub, args)
            return

        await self._handle_group_command(event, sub, args)

    async def _handle_group_command(self, event: AstrMessageEvent, sub: str, args: list[str]) -> None:
        cmd = sub.lower()

        if cmd in {"帮助", "help", "h", "?", "说明"}:
            await event.send(event.plain_result(self._help_text()))
            return

        if cmd in {"开房", "创建", "create"}:
            await self._cmd_create_room(event)
            return

        if cmd in {"加入", "join"}:
            await self._cmd_join_room(event)
            return

        if cmd in {"开始", "start"}:
            await self._cmd_start_room(event)
            return

        if cmd in {"状态", "status", "查看"}:
            await self._cmd_status(event)
            return

        if cmd in {"质疑", "liar", "call"}:
            await self._cmd_challenge(event)
            return

        if cmd in {"剪线", "cut", "wire"}:
            await self._cmd_cut_wire(event, args)
            return

        if cmd in {"结束", "关闭", "end", "exit"}:
            await self._cmd_end_room(event)
            return

        unknown = self._guide("未识别的酒馆指令。", "输入 /酒馆 帮助 查看完整用法。")
        await event.send(event.plain_result(unknown))

    async def _handle_private_command(self, event: AstrMessageEvent, sub: str, args: list[str]) -> None:
        cmd = sub.lower()
        if cmd in {"帮助", "help", "h", "?", "说明"}:
            await event.send(event.plain_result(self._private_help_text()))
            return

        if cmd in {
            "开房",
            "创建",
            "create",
            "加入",
            "join",
            "开始",
            "start",
            "状态",
            "status",
            "查看",
            "质疑",
            "liar",
            "call",
            "剪线",
            "cut",
            "wire",
            "结束",
            "关闭",
            "end",
            "exit",
        }:
            text = self._guide(
                "这些命令只能在群聊中使用，私聊只支持“手牌/出牌”。",
                "请回到房间所在群发送对应群指令。",
            )
            await event.send(event.plain_result(text))
            return

        if cmd in {"手牌", "cards", "hand"}:
            await self._cmd_private_hand(event)
            return

        if cmd in {"出", "play"}:
            await self._cmd_private_play(event, args)
            return

        text = self._guide("私聊仅支持“手牌/出牌”相关操作。", "输入 /酒馆 帮助 查看私聊指令。")
        await event.send(event.plain_result(text))

    async def _cmd_create_room(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        user_name = event.get_sender_name()
        room_id = event.unified_msg_origin
        group_id = str(event.get_group_id() or "")
        platform_id = self._platform_id_from_umo(room_id)
        bot_id = str(getattr(event, "get_self_id", lambda: "")( ))

        async with self.state_lock:
            conflict_room = self.player_room_index.get(user_id)
            if conflict_room and conflict_room != room_id:
                msg = self._guide(
                    f"你已在其他房间中（群 {self._room_group_hint(conflict_room)}），不能重复开房。",
                    "先在原房间用 /酒馆 结束，或等待房间结束后再加入新房。",
                )
                await event.send(event.plain_result(msg))
                return

            if room_id in self.rooms:
                room = self.rooms[room_id]
                msg = self._guide(
                    f"本群已经存在酒馆房间，当前人数 {len(room.order)}。",
                    "其他玩家请用 /酒馆 加入，房主人数够后用 /酒馆 开始。",
                )
                await event.send(event.plain_result(msg))
                return

            now = time.time()
            room = RoomState(
                room_id=room_id,
                group_umo=room_id,
                group_id=group_id,
                platform_id=platform_id,
                bot_id=bot_id,
                owner_id=user_id,
                owner_name=user_name,
                created_at=now,
                updated_at=now,
            )
            room.players[user_id] = PlayerState(user_id=user_id, name=user_name)
            room.order.append(user_id)

            self.rooms[room_id] = room
            self.player_room_index[user_id] = room_id
            await self._save_state_locked()

        msg = self._guide(
            f"开房成功。你是房主，当前 1/5 人。\n{self._room_create_card_intro()}",
            "让其他人发送 /酒馆 加入；人数达到 3~5 人后你发送 /酒馆 开始。",
        )
        await event.send(event.plain_result(msg))

    async def _cmd_join_room(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        user_name = event.get_sender_name()
        room_id = event.unified_msg_origin

        async with self.state_lock:
            room = self.rooms.get(room_id)
            if not room:
                msg = self._guide("本群当前没有房间。", "先由一名玩家发送 /酒馆 开房。")
                await event.send(event.plain_result(msg))
                return

            conflict_room = self.player_room_index.get(user_id)
            if conflict_room and conflict_room != room_id:
                msg = self._guide(
                    f"你已在其他房间中（群 {self._room_group_hint(conflict_room)}）。",
                    "请先结束原房间后再加入。",
                )
                await event.send(event.plain_result(msg))
                return

            if room.phase != PHASE_WAITING:
                msg = self._guide("当前房间已开始，不能中途加入。", "等待本局结束后再开新房。")
                await event.send(event.plain_result(msg))
                return

            if user_id in room.players:
                msg = self._guide("你已经在房间中了。", "房主可在人数达标后发送 /酒馆 开始。")
                await event.send(event.plain_result(msg))
                return

            if len(room.order) >= 5:
                msg = self._guide("房间人数已满（5人）。", "可等待下一局或由房主 /酒馆 结束 后重开。")
                await event.send(event.plain_result(msg))
                return

            room.players[user_id] = PlayerState(user_id=user_id, name=user_name)
            room.order.append(user_id)
            room.updated_at = time.time()
            self.player_room_index[user_id] = room_id
            await self._save_state_locked()

            count = len(room.order)
            owner_name = room.players.get(room.owner_id).name if room.owner_id in room.players else room.owner_name

        msg = self._guide(
            f"加入成功，当前 {count}/5 人。房主：{owner_name}",
            "人数达到 3~5 后，房主发送 /酒馆 开始。",
        )
        await event.send(event.plain_result(msg))

    async def _cmd_start_room(self, event: AstrMessageEvent) -> None:
        room_id = event.unified_msg_origin
        user_id = str(event.get_sender_id())

        async with self.state_lock:
            room = self.rooms.get(room_id)
            if not room:
                msg = self._guide("本群还没有房间。", "先发送 /酒馆 开房。")
                await event.send(event.plain_result(msg))
                return

            if room.phase != PHASE_WAITING:
                msg = self._guide("房间已在进行中。", "可用 /酒馆 状态 查看当前进度。")
                await event.send(event.plain_result(msg))
                return

            if user_id != room.owner_id:
                msg = self._guide("只有房主可以开始。", "请房主发送 /酒馆 开始。")
                await event.send(event.plain_result(msg))
                return

            if len(room.order) < 3:
                msg = self._guide("人数不足，至少需要 3 人。", "让更多玩家发送 /酒馆 加入。")
                await event.send(event.plain_result(msg))
                return

            if len(room.order) > 5:
                msg = self._guide("人数超过上限 5 人。", "请房主 /酒馆 结束 后重新开房。")
                await event.send(event.plain_result(msg))
                return

            need_check = bool(self.conf.get("require_dm_reachable_before_start", True))
            failed: list[str] = []
            if need_check:
                for uid in room.order:
                    ok = await self._probe_private_reachable(room, uid)
                    room.players[uid].dm_reachable = ok
                    if not ok:
                        failed.append(uid)

            if failed:
                chain: list[Any] = [Comp.Plain("以下玩家私聊不可达，请先加好友并私聊机器人后重试：")]
                for uid in failed:
                    chain.append(Comp.Plain(" "))
                    chain.append(Comp.At(qq=uid))
                chain.append(Comp.Plain("\n下一步：处理后由房主再次发送 /酒馆 开始。"))
                await self._send_to_umo(room.group_umo, chain)
                room.updated_at = time.time()
                await self._save_state_locked()
                return

            room.phase = PHASE_PLAYING
            room.started_at = time.time()
            room.round_no = 0
            room.dealer_cursor = random.randint(0, max(0, len(room.order) - 1))
            room.last_play = None
            room.pending_wire_user_id = ""
            room.wire_options = []
            room.wire_index_map = {}
            room.initial_player_count = len(room.order)
            room.fixed_hand_size = FIXED_HAND_SIZE
            room.round_deck_counts = self._build_locked_deck_counts(room.initial_player_count)
            room.round_deck_total = sum(room.round_deck_counts.values())
            room.play_deadline_ts = 0
            room.wire_deadline_ts = 0

            for uid in room.order:
                room.players[uid].reset_for_new_game()

            await self._send_rules_forward(room)
            await self._start_new_round_locked(room, reason="大局开始")
            room.updated_at = time.time()
            await self._save_state_locked()

    async def _cmd_status(self, event: AstrMessageEvent) -> None:
        room_id = event.unified_msg_origin
        async with self.state_lock:
            room = self.rooms.get(room_id)
            if not room:
                msg = self._guide("本群没有进行中的酒馆房间。", "先发送 /酒馆 开房。")
                await event.send(event.plain_result(msg))
                return

            alive = room.alive_ids()
            lines = [
                f"房间状态：{self._phase_label(room.phase)}",
                f"房主：{room.players.get(room.owner_id).name if room.owner_id in room.players else room.owner_name}",
                f"玩家数：{len(room.order)}（存活 {len(alive)}）",
                f"小局：第 {room.round_no} 局",
            ]

            if room.phase in {PHASE_PLAYING, PHASE_AWAIT_WIRE} and room.target_card:
                lines.append(f"当前目标牌：{CARD_NAME.get(room.target_card, room.target_card)}")

            if room.phase == PHASE_PLAYING and room.current_turn_user_id:
                pname = room.players.get(room.current_turn_user_id).name if room.current_turn_user_id in room.players else room.current_turn_user_id
                lines.append(f"当前行动：{pname}（私聊 /酒馆 出 序号...）")

            if room.phase == PHASE_AWAIT_WIRE and room.pending_wire_user_id:
                pname = room.players.get(room.pending_wire_user_id).name if room.pending_wire_user_id in room.players else room.pending_wire_user_id
                opts = ", ".join([f"{idx}={color}" for idx, color in room.wire_index_map.items()])
                lines.append(f"待剪线：{pname}（{opts}）")

            lines.append("玩家信息：")
            for uid in room.order:
                p = room.players.get(uid)
                if not p:
                    continue
                status = "存活" if p.alive else "出局"
                lines.append(f"- {p.name}：{status}，手牌 {len(p.hand)}")

            guide = "可用 /酒馆 质疑、/酒馆 剪线、/酒馆 结束 或 /酒馆 帮助。"
            msg = self._guide("\n".join(lines), guide)
            await event.send(event.plain_result(msg))

    async def _cmd_challenge(self, event: AstrMessageEvent) -> None:
        room_id = event.unified_msg_origin
        challenger_id = str(event.get_sender_id())

        async with self.state_lock:
            room = self.rooms.get(room_id)
            if not room:
                msg = self._guide("本群没有房间。", "先发送 /酒馆 开房。")
                await event.send(event.plain_result(msg))
                return

            if room.phase != PHASE_PLAYING:
                msg = self._guide("当前不是质疑阶段。", "可用 /酒馆 状态 查看当前需要的动作。")
                await event.send(event.plain_result(msg))
                return

            if not room.last_play:
                msg = self._guide("还没有可质疑的上一手出牌。", "等待当前行动玩家先私聊出牌。")
                await event.send(event.plain_result(msg))
                return

            if challenger_id != room.current_turn_user_id:
                current_name = room.players.get(room.current_turn_user_id).name if room.current_turn_user_id in room.players else room.current_turn_user_id
                msg = self._guide(f"现在应由 {current_name} 决定是否质疑。", "请等待轮到你。")
                await event.send(event.plain_result(msg))
                return

            await self._resolve_challenge_locked(room, challenger_id, auto=False)
            room.updated_at = time.time()
            await self._save_state_locked()

    async def _cmd_cut_wire(self, event: AstrMessageEvent, args: list[str]) -> None:
        room_id = event.unified_msg_origin
        user_id = str(event.get_sender_id())

        if not args:
            msg = self._guide("用法：/酒馆 剪线 红|蓝|黄（也支持 1/2/3）", "先用 /酒馆 状态 查看当前可选线。")
            await event.send(event.plain_result(msg))
            return

        async with self.state_lock:
            room = self.rooms.get(room_id)
            if not room:
                msg = self._guide("本群没有房间。", "先发送 /酒馆 开房。")
                await event.send(event.plain_result(msg))
                return

            if room.phase != PHASE_AWAIT_WIRE:
                msg = self._guide("当前不是剪线阶段。", "可用 /酒馆 状态 查看当前阶段。")
                await event.send(event.plain_result(msg))
                return

            if user_id != room.pending_wire_user_id:
                pname = room.players.get(room.pending_wire_user_id).name if room.pending_wire_user_id in room.players else room.pending_wire_user_id
                msg = self._guide(f"当前应由 {pname} 剪线。", "请等待该玩家操作。")
                await event.send(event.plain_result(msg))
                return

            picked = self._resolve_wire_arg(args[0], room)
            if not picked:
                opts = " / ".join([f"{k}={v}" for k, v in room.wire_index_map.items()])
                msg = self._guide(f"线名无效。当前可选：{opts}", "请重新发送 /酒馆 剪线 红|蓝|黄。")
                await event.send(event.plain_result(msg))
                return

            await self._apply_wire_cut_locked(room, user_id, picked, by_timeout=False)
            room.updated_at = time.time()
            await self._save_state_locked()

    async def _cmd_end_room(self, event: AstrMessageEvent) -> None:
        room_id = event.unified_msg_origin
        user_id = str(event.get_sender_id())
        is_admin = bool(getattr(event, "is_admin", lambda: False)())

        async with self.state_lock:
            room = self.rooms.get(room_id)
            if not room:
                msg = self._guide("本群没有房间可结束。", "如需新开一局，请发送 /酒馆 开房。")
                await event.send(event.plain_result(msg))
                return

            if user_id != room.owner_id and not is_admin:
                msg = self._guide("仅房主或管理员可以结束房间。", "请让房主发送 /酒馆 结束。")
                await event.send(event.plain_result(msg))
                return

            ender = room.players.get(user_id).name if user_id in room.players else event.get_sender_name()
            await self._send_group_text(
                room,
                self._guide(
                    f"房间已结束（操作人：{ender}）。",
                    "如需再玩，请发送 /酒馆 开房。",
                ),
            )
            self._drop_room_locked(room.room_id)
            await self._save_state_locked()

    async def _cmd_private_hand(self, event: AstrMessageEvent) -> None:
        user_id = str(event.get_sender_id())
        async with self.state_lock:
            room_id = self.player_room_index.get(user_id)
            if not room_id or room_id not in self.rooms:
                msg = self._guide("你当前不在任何酒馆房间。", "先去群里发送 /酒馆 开房 或 /酒馆 加入。")
                await event.send(event.plain_result(msg))
                return

            room = self.rooms[room_id]
            if user_id not in room.players:
                msg = self._guide("房间状态异常，未找到你的玩家信息。", "请通知房主 /酒馆 结束 后重开。")
                await event.send(event.plain_result(msg))
                return

            try:
                await self._send_private_hand(room, user_id, force_tip=True)
            except Exception:
                msg = self._guide("私聊消息发送失败。", "请先加好友并私聊机器人一次后重试。")
                await event.send(event.plain_result(msg))

    async def _cmd_private_play(self, event: AstrMessageEvent, args: list[str]) -> None:
        user_id = str(event.get_sender_id())

        async with self.state_lock:
            room_id = self.player_room_index.get(user_id)
            if not room_id or room_id not in self.rooms:
                msg = self._guide("你当前不在任何酒馆房间。", "先去群里 /酒馆 开房 或 /酒馆 加入。")
                await event.send(event.plain_result(msg))
                return

            room = self.rooms[room_id]
            if room.phase != PHASE_PLAYING:
                msg = self._guide("当前不是出牌阶段。", "可在群里用 /酒馆 状态 查看当前需要动作。")
                await event.send(event.plain_result(msg))
                return

            player = room.players.get(user_id)
            if not player:
                msg = self._guide("玩家信息缺失。", "请通知房主 /酒馆 结束 后重开。")
                await event.send(event.plain_result(msg))
                return

            if not player.alive:
                msg = self._guide("你已出局，不能继续出牌。", "等待本局结束后再开新房。")
                await event.send(event.plain_result(msg))
                return

            if user_id != room.current_turn_user_id:
                name = room.players.get(room.current_turn_user_id).name if room.current_turn_user_id in room.players else room.current_turn_user_id
                msg = self._guide(f"现在轮到 {name}。", "请等待轮到你后再私聊 /酒馆 出。")
                await event.send(event.plain_result(msg))
                return

            if not args:
                msg = self._guide("用法：/酒馆 出 2 4 5", "先发送 /酒馆 手牌 查看序号。")
                await event.send(event.plain_result(msg))
                return

            parsed = self._parse_indices(args, len(player.hand))
            if isinstance(parsed, str):
                msg = self._guide(parsed, "先发送 /酒馆 手牌 查看正确序号后再提交。")
                await event.send(event.plain_result(msg))
                return

            indices = parsed
            played_cards = [player.hand[i - 1] for i in indices]
            for idx in sorted(indices, reverse=True):
                player.hand.pop(idx - 1)

            room.last_play = LastPlay(
                player_id=user_id,
                cards=played_cards,
                declared_target=room.target_card,
                played_at=time.time(),
            )

            next_uid = self._next_alive_after(room, user_id)
            if not next_uid:
                await self._announce_winner_and_close_locked(room, reason="其他玩家已全部出局")
                await self._save_state_locked()
                return

            declared = CARD_NAME.get(room.target_card, room.target_card)
            card_count = len(played_cards)
            pname = player.name
            next_name = room.players[next_uid].name if next_uid in room.players else next_uid

            await self._send_group_text(
                room,
                self._guide(
                    f"{pname} 已暗出 {card_count} 张，并宣称【{declared}】。",
                    f"现在由 {next_name} 决定：群里 /酒馆 质疑，或在私聊直接出牌。",
                ),
            )

            if len(player.hand) == 0:
                room.current_turn_user_id = next_uid
                await self._send_group_text(
                    room,
                    f"{pname} 已出完手牌，系统自动触发下家质疑。",
                )
                await self._resolve_challenge_locked(room, next_uid, auto=True)
                room.updated_at = time.time()
                await self._save_state_locked()
                return

            room.current_turn_user_id = next_uid
            room.phase = PHASE_PLAYING
            room.pending_wire_user_id = ""
            room.wire_options = []
            room.wire_index_map = {}
            room.wire_deadline_ts = 0
            room.action_token += 1
            room.play_deadline_ts = time.time() + self._play_timeout_seconds()
            self._arm_play_timeout_task(room.room_id, room.action_token, room.current_turn_user_id, room.play_deadline_ts)

            await self._send_private_hand(room, user_id, force_tip=True)
            room.updated_at = time.time()
            await self._save_state_locked()

            done_msg = self._guide(
                f"已提交：你本次暗出 {card_count} 张。",
                "等待群内结算，或稍后再用 /酒馆 手牌 查看最新手牌。",
            )
            await event.send(event.plain_result(done_msg))

    async def _resolve_challenge_locked(self, room: RoomState, challenger_id: str, auto: bool) -> None:
        if not room.last_play:
            return

        last = room.last_play
        liar = any(card not in {room.target_card, CARD_MAGIC} for card in last.cards)
        punished_id = last.player_id if liar else challenger_id

        revealer = room.players.get(last.player_id).name if last.player_id in room.players else last.player_id
        challenger = room.players.get(challenger_id).name if challenger_id in room.players else challenger_id
        revealed_cards = "、".join(CARD_NAME.get(c, c) for c in last.cards)

        if liar:
            result = f"质疑成立：{revealer} 本次暗牌包含非目标牌。"
        else:
            result = f"质疑失败：{revealer} 本次暗牌全为目标牌/魔术牌。"

        auto_prefix = "[系统自动质疑] " if auto else ""
        await self._send_group_text(
            room,
            f"{auto_prefix}翻牌结果：{revealed_cards}\n发起方：{challenger}\n{result}",
        )

        punished = room.players.get(punished_id)
        if not punished or not punished.alive:
            await self._send_group_text(room, "受罚玩家状态异常，直接进入下一小局。")
            await self._start_new_round_locked(room, reason="异常恢复")
            return

        room.phase = PHASE_AWAIT_WIRE
        room.pending_wire_user_id = punished_id
        room.wire_options = [c for c in WIRE_COLORS if c in punished.wires_remaining]
        room.wire_index_map = {str(i + 1): color for i, color in enumerate(room.wire_options)}
        room.play_deadline_ts = 0
        self._cancel_play_timeout_task(room.room_id)

        room.action_token += 1
        room.wire_deadline_ts = time.time() + self._wire_timeout_seconds()
        self._arm_wire_timeout_task(room.room_id, room.action_token, punished_id, room.wire_deadline_ts)

        options_text = " / ".join([f"{k}={v}" for k, v in room.wire_index_map.items()])
        pname = punished.name

        bomb_img = self.renderer.bomb_path_for_options(room.wire_options)
        chain: list[Any] = [Comp.At(qq=punished_id), Comp.Plain(f" {pname} 进入剪线阶段。可选：{options_text}\n")]
        if bomb_img and bomb_img.exists():
            chain.append(Comp.Image.fromFileSystem(str(bomb_img)))
        chain.append(
            Comp.Plain(
                self._guide(
                    "",
                    "受罚玩家请发送 /酒馆 剪线 红|蓝|黄（也支持数字 1/2/3）。",
                )
            )
        )
        await self._send_to_umo(room.group_umo, chain)

    async def _apply_wire_cut_locked(self, room: RoomState, user_id: str, color: str, by_timeout: bool) -> None:
        player = room.players.get(user_id)
        if not player:
            return

        options = room.wire_options.copy()
        if color not in options:
            return

        if color in player.wires_remaining:
            player.wires_remaining.remove(color)

        exploded = color == player.bomb_color or len(options) == 1

        room.phase = PHASE_PLAYING
        room.pending_wire_user_id = ""
        room.wire_options = []
        room.wire_index_map = {}
        room.wire_deadline_ts = 0
        self._cancel_wire_timeout_task(room.room_id)

        who = player.name
        prefix = "[超时自动剪线] " if by_timeout else ""

        if exploded:
            player.alive = False
            player.hand = []
            chain: list[Any] = [Comp.Plain(f"{prefix}{who} 剪到【{color}线】并触发爆炸，已出局。")]
            explode = self.renderer.bomb_explode_path()
            if explode.exists():
                chain.append(Comp.Image.fromFileSystem(str(explode)))
            chain.append(Comp.Plain(self._guide("", "本小局结束，系统将开始下一小局。")))
            await self._send_to_umo(room.group_umo, chain)
        else:
            remain = "、".join(player.wires_remaining)
            await self._send_group_text(
                room,
                self._guide(
                    f"{prefix}{who} 剪到【{color}线】并安全通过。剩余线：{remain}",
                    "本小局结束，系统将开始下一小局。",
                ),
            )

        alive = room.alive_ids()
        if len(alive) <= 1:
            await self._announce_winner_and_close_locked(room, reason="决出唯一幸存者")
            return

        await self._start_new_round_locked(room, reason="剪线结算后")

    async def _start_new_round_locked(self, room: RoomState, reason: str) -> None:
        alive_ids = room.alive_ids()
        if len(alive_ids) <= 1:
            await self._announce_winner_and_close_locked(room, reason="仅剩一名玩家")
            return

        for uid in room.order:
            if uid in room.players:
                room.players[uid].hand = []

        hand_size = max(1, int(room.fixed_hand_size or FIXED_HAND_SIZE))
        deck = self._build_round_deck(room)
        need = hand_size * len(alive_ids)
        if len(deck) < need:
            await self._send_group_text(
                room,
                self._guide("牌池数据异常，无法继续发牌，已终止本局。", "请重新发送 /酒馆 开房。"),
            )
            self._drop_room_locked(room.room_id)
            return
        for uid in alive_ids:
            take = deck[:hand_size]
            deck = deck[hand_size:]
            room.players[uid].hand = take

        room.round_no += 1
        room.target_card = random.choice(TARGET_CARDS)

        starter = alive_ids[room.dealer_cursor % len(alive_ids)]
        room.dealer_cursor += 1
        room.current_turn_user_id = starter
        room.last_play = None
        room.phase = PHASE_PLAYING
        room.pending_wire_user_id = ""
        room.wire_options = []
        room.wire_index_map = {}
        room.wire_deadline_ts = 0

        room.action_token += 1
        room.play_deadline_ts = time.time() + self._play_timeout_seconds()
        self._arm_play_timeout_task(room.room_id, room.action_token, starter, room.play_deadline_ts)
        self._cancel_wire_timeout_task(room.room_id)

        target_name = CARD_NAME.get(room.target_card, room.target_card)
        starter_name = room.players.get(starter).name if starter in room.players else starter

        pool_text = self._card_pool_text(room)
        chain: list[Any] = [Comp.Plain(f"第 {room.round_no} 小局开始（{reason}）。目标牌：{target_name}\n{pool_text}\n")]
        target_img = self.renderer.target_path(room.target_card)
        if target_img.exists():
            chain.append(Comp.Image.fromFileSystem(str(target_img)))
        chain.append(
            Comp.Plain(
                self._guide(
                    "",
                    f"由 {starter_name} 先行动：请该玩家私聊 /酒馆 出 序号...；其他人等待质疑时机。",
                )
            )
        )
        await self._send_to_umo(room.group_umo, chain)

        for uid in alive_ids:
            try:
                await self._send_private_hand(room, uid, force_tip=False)
            except Exception:
                # Avoid breaking the whole round flow if one player's private channel is unavailable.
                await self._send_to_umo(
                    room.group_umo,
                    [
                        Comp.At(qq=uid),
                        Comp.Plain(" 私聊发牌失败，请先加好友并私聊机器人一次。"),
                    ],
                )

    async def _announce_winner_and_close_locked(self, room: RoomState, reason: str) -> None:
        alive_ids = room.alive_ids()
        if alive_ids:
            winner_id = alive_ids[0]
            winner_name = room.players.get(winner_id).name if winner_id in room.players else winner_id
            text = self._guide(
                f"大局结束：{winner_name} 获胜。\n原因：{reason}",
                "如需再玩，请发送 /酒馆 开房。",
            )
        else:
            text = self._guide("大局结束：无幸存者。", "如需再玩，请发送 /酒馆 开房。")
        await self._send_group_text(room, text)
        self._drop_room_locked(room.room_id)

    async def _send_private_hand(self, room: RoomState, user_id: str, force_tip: bool) -> None:
        player = room.players.get(user_id)
        if not player:
            return

        if not player.alive:
            await self._send_private_text(
                room,
                user_id,
                self._guide("你已出局，当前无法出牌。", "等待本局结束后在群里重新开房。"),
            )
            return

        hand_img = self.renderer.build_hand_image(
            room_id=room.room_id,
            user_id=user_id,
            cards=player.hand,
            width_hint=int(self.conf.get("hand_image_width", 960)),
        )

        card_list = []
        for idx, code in enumerate(player.hand, start=1):
            card_list.append(f"{idx}:{CARD_NAME.get(code, code)}")

        info = (
            f"当前房间群号：{room.group_id}\n"
            f"目标牌：{CARD_NAME.get(room.target_card, room.target_card)}\n"
            f"你的手牌：{'  '.join(card_list) if card_list else '无'}"
        )

        next_tip = "若轮到你，发送 /酒馆 出 2 4 5。也可随时发送 /酒馆 手牌 重新查看。"
        if force_tip:
            info = self._guide(info, next_tip)
        elif bool(self.conf.get("guide_mode", True)):
            info = f"{info}\n\n下一步：{next_tip}"

        await self._send_private_text(room, user_id, info, image_path=hand_img)

    async def _probe_private_reachable(self, room: RoomState, user_id: str) -> bool:
        text = "[酒馆连通检查] 收到这条消息代表私聊通道可用。"
        try:
            await self._send_private_text(room, user_id, text)
            return True
        except Exception:
            return False

    async def _send_rules_forward(self, room: RoomState) -> None:
        pool_text = self._card_pool_text(room)
        play_timeout = self._play_timeout_seconds()
        wire_timeout = self._wire_timeout_seconds()
        sections = [
            "【酒馆基础规则】\n1) 每小局随机目标牌（太阳/月亮/星星），并给当前存活玩家每人固定发 5 张",
            "2) 出牌在私聊完成，群里只公布宣称数量",
            "3) 质疑规则：下一位可在群里 /酒馆 质疑\n4) 判定规则：暗牌中“任一假即判假”，魔术牌可当目标牌",
            "5) 受罚进入剪线：三选一 -> 二选一 -> 一选一必爆\n6) 命令：/酒馆 剪线 红|蓝|黄（支持数字）",
            f"7) 超时：出牌{play_timeout}秒超时直接整局淘汰；剪线{wire_timeout}秒超时自动剪线",
            "8) 若玩家出完手牌，下家会被系统自动触发质疑",
            "9) 全程一人一房，避免私聊串局\n10) 所有命令可随时用 /酒馆 帮助 查询",
            f"11) 本大局牌池在开局时一次锁定，后续小局不变化。\n{pool_text}",
        ]

        try:
            bot_uin = int(room.bot_id) if room.bot_id.isdigit() else int(room.owner_id)
            nodes = [Comp.Node(uin=bot_uin, name="酒馆规则", content=[Comp.Plain(s)]) for s in sections]
            await self._send_to_umo(room.group_umo, [Comp.Nodes(nodes=nodes)])
        except Exception:
            fallback = "\n\n".join(sections)
            await self._send_group_text(room, fallback)

    async def _handle_play_timeout(self, room_id: str, token: int, target_uid: str) -> None:
        async with self.state_lock:
            room = self.rooms.get(room_id)
            if not room:
                return
            if room.phase != PHASE_PLAYING:
                return
            if room.action_token != token:
                return
            if room.current_turn_user_id != target_uid:
                return
            if time.time() < room.play_deadline_ts - 0.2:
                return

            player = room.players.get(target_uid)
            if not player or not player.alive:
                return

            player.alive = False
            player.hand = []
            room.play_deadline_ts = 0
            self._cancel_play_timeout_task(room_id)

            await self._send_to_umo(
                room.group_umo,
                [
                    Comp.At(qq=target_uid),
                    Comp.Plain(
                        f" 出牌超时 {self._play_timeout_seconds()} 秒，直接整局淘汰。\n"
                        + self._guide("", "本小局结束，系统将自动开始下一小局。")
                    ),
                ],
            )

            if len(room.alive_ids()) <= 1:
                await self._announce_winner_and_close_locked(room, reason="超时淘汰后仅剩一人")
                await self._save_state_locked()
                return

            await self._start_new_round_locked(room, reason="超时淘汰结算")
            room.updated_at = time.time()
            await self._save_state_locked()

    async def _handle_wire_timeout(self, room_id: str, token: int, target_uid: str) -> None:
        async with self.state_lock:
            room = self.rooms.get(room_id)
            if not room:
                return
            if room.phase != PHASE_AWAIT_WIRE:
                return
            if room.action_token != token:
                return
            if room.pending_wire_user_id != target_uid:
                return
            if time.time() < room.wire_deadline_ts - 0.2:
                return

            if not room.wire_options:
                await self._start_new_round_locked(room, reason="剪线状态修复")
                await self._save_state_locked()
                return

            picked = random.choice(room.wire_options)
            await self._apply_wire_cut_locked(room, target_uid, picked, by_timeout=True)
            room.updated_at = time.time()
            await self._save_state_locked()

    def _arm_play_timeout_task(self, room_id: str, token: int, target_uid: str, deadline_ts: float) -> None:
        self._cancel_play_timeout_task(room_id)
        delay = max(0.0, deadline_ts - time.time())

        async def runner():
            try:
                await asyncio.sleep(delay)
                await self._handle_play_timeout(room_id, token, target_uid)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(f"酒馆出牌超时任务异常: {exc}")

        self.play_timeout_tasks[room_id] = asyncio.create_task(runner())

    def _arm_wire_timeout_task(self, room_id: str, token: int, target_uid: str, deadline_ts: float) -> None:
        self._cancel_wire_timeout_task(room_id)
        delay = max(0.0, deadline_ts - time.time())

        async def runner():
            try:
                await asyncio.sleep(delay)
                await self._handle_wire_timeout(room_id, token, target_uid)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(f"酒馆剪线超时任务异常: {exc}")

        self.wire_timeout_tasks[room_id] = asyncio.create_task(runner())

    def _cancel_play_timeout_task(self, room_id: str) -> None:
        task = self.play_timeout_tasks.pop(room_id, None)
        current_task: Optional[asyncio.Task] = None
        with contextlib.suppress(RuntimeError):
            current_task = asyncio.current_task()
        if task and task is not current_task:
            task.cancel()

    def _cancel_wire_timeout_task(self, room_id: str) -> None:
        task = self.wire_timeout_tasks.pop(room_id, None)
        current_task: Optional[asyncio.Task] = None
        with contextlib.suppress(RuntimeError):
            current_task = asyncio.current_task()
        if task and task is not current_task:
            task.cancel()

    def _drop_room_locked(self, room_id: str) -> None:
        room = self.rooms.pop(room_id, None)
        if not room:
            return

        self._cancel_play_timeout_task(room_id)
        self._cancel_wire_timeout_task(room_id)

        for uid, rid in list(self.player_room_index.items()):
            if rid == room_id:
                self.player_room_index.pop(uid, None)

    async def _resume_timers(self) -> None:
        async with self.state_lock:
            for room in self.rooms.values():
                if room.phase == PHASE_PLAYING and room.current_turn_user_id and room.play_deadline_ts > 0:
                    self._arm_play_timeout_task(
                        room.room_id,
                        room.action_token,
                        room.current_turn_user_id,
                        room.play_deadline_ts,
                    )
                elif room.phase == PHASE_AWAIT_WIRE and room.pending_wire_user_id and room.wire_deadline_ts > 0:
                    self._arm_wire_timeout_task(
                        room.room_id,
                        room.action_token,
                        room.pending_wire_user_id,
                        room.wire_deadline_ts,
                    )

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(300)
                await self._cleanup_rooms()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"酒馆清理任务异常: {exc}")

    async def _cleanup_rooms(self) -> None:
        ttl_seconds = max(60, int(self.conf.get("room_ttl_minutes", 180)) * 60)
        now = time.time()
        async with self.state_lock:
            stale = [
                rid
                for rid, room in self.rooms.items()
                if room.phase == PHASE_WAITING and now - room.updated_at >= ttl_seconds
            ]
            if not stale:
                return

            for rid in stale:
                room = self.rooms.get(rid)
                if room:
                    await self._send_group_text(room, "房间等待超时已自动关闭。")
                self._drop_room_locked(rid)

            await self._save_state_locked()

    async def _send_group_text(self, room: RoomState, text: str) -> None:
        await self._send_to_umo(room.group_umo, [Comp.Plain(text)])

    async def _send_private_text(
        self,
        room: RoomState,
        user_id: str,
        text: str,
        image_path: Optional[Path] = None,
    ) -> None:
        session = self._private_umo(room.platform_id, user_id)
        chain: list[Any] = [Comp.Plain(text)]
        if image_path and image_path.exists():
            chain.append(Comp.Image.fromFileSystem(str(image_path)))
        await self._send_to_umo(session, chain)

    async def _send_to_umo(self, umo: str, chain: list[Any]) -> None:
        await self.context.send_message(umo, MessageChain(chain))

    def _private_umo(self, platform_id: str, user_id: str) -> str:
        return str(MessageSession(platform_id, MessageType.FRIEND_MESSAGE, str(user_id)))

    def _next_alive_after(self, room: RoomState, user_id: str) -> Optional[str]:
        alive = room.alive_ids()
        if len(alive) <= 1:
            return None
        if user_id not in alive:
            return alive[0]
        idx = alive.index(user_id)
        return alive[(idx + 1) % len(alive)]

    def _parse_indices(self, args: list[str], max_size: int) -> list[int] | str:
        values: list[int] = []
        for item in args:
            if not re.fullmatch(r"\d+", item):
                return "序号必须是正整数。"
            values.append(int(item))
        if not values:
            return "请至少提供一个序号。"
        if len(values) != len(set(values)):
            return "序号不能重复。"
        if any(v <= 0 for v in values):
            return "序号必须大于 0。"
        if any(v > max_size for v in values):
            return f"存在越界序号，当前手牌只有 {max_size} 张。"
        return sorted(values)

    def _resolve_wire_arg(self, raw: str, room: RoomState) -> Optional[str]:
        text = raw.strip().lower()
        if text in room.wire_index_map:
            return room.wire_index_map[text]

        text = text.replace("线", "").replace("色", "")
        if text in WIRE_ALIASES:
            picked = WIRE_ALIASES[text]
            if picked in room.wire_options:
                return picked
        return None

    def _phase_label(self, phase: str) -> str:
        return {
            PHASE_WAITING: "等待开局",
            PHASE_PLAYING: "出牌/质疑",
            PHASE_AWAIT_WIRE: "剪线结算",
        }.get(phase, phase)

    def _strip_command_prefix(self, text: str) -> str:
        for prefix in COMMAND_PREFIXES:
            if text.startswith(prefix):
                return text[len(prefix) :].strip()
        return text

    def _platform_id_from_umo(self, umo: str) -> str:
        if not umo:
            return ""
        return umo.split(":", 1)[0]

    def _room_group_hint(self, room_id: str) -> str:
        room = self.rooms.get(room_id)
        if not room:
            return "未知"
        return room.group_id or "未知"

    def _is_duplicate_event(self, event: AstrMessageEvent) -> bool:
        now = time.time()
        for key, exp in list(self.recent_event_cache.items()):
            if exp < now:
                self.recent_event_cache.pop(key, None)

        message_id = None
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            message_id = getattr(message_obj, "message_id", None) or getattr(message_obj, "msg_id", None)

        if message_id is None:
            return False

        key = f"{event.unified_msg_origin}:{message_id}"
        if key in self.recent_event_cache:
            return True
        self.recent_event_cache[key] = now + 30
        return False

    def _guide(self, body: str, next_step: str) -> str:
        if not bool(self.conf.get("guide_mode", True)):
            return body
        body = body.rstrip()
        if next_step:
            if body:
                return f"{body}\n\n下一步：{next_step}"
            return f"下一步：{next_step}"
        return body

    def _help_text(self) -> str:
        return (
            "【骗子酒馆基础版 帮助】\n"
            "发牌：每小局会清空上局手牌，并给当前存活玩家每人固定发 5 张；牌型=太阳/月亮/星星/魔术。\n"
            "卡池：在开局时按人数一次锁定（3人=15张、4人=20张、5人=25张），本大局后续小局不再变化。\n\n"
            "群指令：\n"
            "- /酒馆 开房：创建房间，发起者自动房主\n"
            "- /酒馆 加入：加入本群房间（3~5人可开）\n"
            "- /酒馆 开始：房主开局\n"
            "- /酒馆 状态：查看阶段/轮次/当前行动\n"
            "- /酒馆 质疑：质疑上一手\n"
            "- /酒馆 剪线 红|蓝|黄（兼容 1/2/3）\n"
            "- /酒馆 结束：房主或管理员随时结束\n"
            "- /酒馆 帮助：查看本说明\n\n"
            "私聊指令：\n"
            "- /酒馆 手牌：查看手牌图和序号\n"
            "- /酒馆 出 2 4 5：一次出多张暗牌\n\n"
            "超时机制：\n"
            f"- 出牌超时：{self._play_timeout_seconds()} 秒（超时整局淘汰）\n"
            f"- 剪线超时：{self._wire_timeout_seconds()} 秒（超时自动剪线）\n\n"
            "常用流程：\n"
            "开房 -> 加入 -> 开始 -> 私聊出牌 -> 群里质疑/剪线\n\n"
            "常见问题：\n"
            "1) 私聊不可达：请先加好友并私聊机器人一次\n"
            "2) 一人一房：同一时间只能在一个群房间中\n"
            "3) 非当前回合：请先 /酒馆 状态 查看行动人\n"
            "4) 私聊不能开房/加入：这类命令只能在群聊使用\n\n"
            "下一步：在群里发送 /酒馆 开房 开始游戏。"
        )

    def _private_help_text(self) -> str:
        return (
            "【酒馆私聊指令】\n"
            "- /酒馆 手牌\n"
            "- /酒馆 出 2 4 5\n\n"
            "说明：\n"
            "1) 只有轮到你时才能出牌\n"
            "2) 可一次出多张，序号不可重复\n"
            "3) 出牌后去群里看质疑/结算\n"
            "4) 开房/加入/开始/质疑/剪线/结束必须在群里执行\n\n"
            "下一步：先发送 /酒馆 手牌 查看当前序号。"
        )

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            logger.error(f"酒馆读取状态失败: {exc}")
            return

        rooms_raw = raw.get("rooms", {}) if isinstance(raw, dict) else {}
        loaded_rooms: dict[str, RoomState] = {}
        for rid, item in rooms_raw.items():
            if not isinstance(item, dict):
                continue
            room = RoomState.from_dict(item)
            if not room.room_id:
                room.room_id = str(rid)
            self._normalize_room_state(room)
            loaded_rooms[room.room_id] = room

        self.rooms = loaded_rooms
        self.player_room_index = {
            str(uid): str(rid)
            for uid, rid in (raw.get("player_room_index", {}) or {}).items()
            if str(rid) in self.rooms
        }

        # rebuild index to avoid stale mapping
        for room in self.rooms.values():
            for uid in room.order:
                self.player_room_index[str(uid)] = room.room_id

    def _normalize_room_state(self, room: RoomState) -> None:
        # Keep order/player structures consistent even if persisted state is edited or partially corrupted.
        seen: set[str] = set()
        cleaned_order: list[str] = []
        for uid in room.order:
            suid = str(uid)
            if suid in room.players and suid not in seen:
                cleaned_order.append(suid)
                seen.add(suid)
        if not cleaned_order:
            cleaned_order = [str(uid) for uid in room.players.keys()]
        room.order = cleaned_order

        if room.owner_id not in room.players and room.order:
            room.owner_id = room.order[0]
            room.owner_name = room.players[room.owner_id].name

        if room.current_turn_user_id and room.current_turn_user_id not in room.players:
            room.current_turn_user_id = room.order[0] if room.order else ""

        if room.pending_wire_user_id and room.pending_wire_user_id not in room.players:
            room.pending_wire_user_id = ""
            room.wire_options = []
            room.wire_index_map = {}
            room.wire_deadline_ts = 0

        if room.last_play and room.last_play.player_id not in room.players:
            room.last_play = None

        if room.fixed_hand_size <= 0:
            room.fixed_hand_size = FIXED_HAND_SIZE

        if room.initial_player_count <= 0 and room.order:
            room.initial_player_count = len(room.order)

        need_rebuild = room.round_deck_total <= 0 or not room.round_deck_counts
        if need_rebuild and room.phase != PHASE_WAITING:
            base_players = room.initial_player_count if room.initial_player_count > 0 else max(3, min(5, len(room.order)))
            room.round_deck_counts = self._build_locked_deck_counts(base_players)
            room.round_deck_total = sum(room.round_deck_counts.values())

    async def _save_state_locked(self) -> None:
        payload = {
            "rooms": {rid: room.to_dict() for rid, room in self.rooms.items()},
            "player_room_index": self.player_room_index,
        }
        tmp = self.state_path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as exc:
            logger.error(f"酒馆保存状态失败: {exc}")
            with contextlib.suppress(Exception):
                tmp.unlink()
