"""Model-facing K-pop comeback trail tool: reconstruct one release era from YouTube content.

The video metadata comes from `tools/common/youtube.py`. This tool's own work
is on top of that: it filters unrelated and fan-uploaded results, classifies
videos into K-pop comeback content types, places them into comeback phases,
and orders the surviving videos into the promotional trail fans actually
follow around a release.

The input may be an album/single directly, or a release resolved from a song
the user already knows. YouTube search is only the data source; the comeback
taxonomy and era reconstruction live here.
"""

import json
import re

from tools.common import youtube

SOURCE = "youtube"
MIN_ARTIST_CHARS = 2
MIN_RELEASE_CHARS = 1
SEARCH_RESULTS = 25

# --- What a video title tells us ---

CONTENT_RULES = {
    "mv_teaser": {
        "phase": "pre_release",
        "words": ["mv teaser", "music video teaser"],
    },
    "highlight_medley": {
        "phase": "pre_release",
        "words": ["highlight medley", "album sampler"],
    },
    "concept_or_trailer": {
        "phase": "pre_release",
        "words": ["concept film", "concept video", "trailer", "intro"],
    },
    "official_mv": {
        "phase": "release",
        "words": ["official mv", "official music video", "' mv", "\" mv"],
    },
        "track_or_live_clip": {
        "phase": "release",
        "words": ["track video", "live clip", "special video"],
    },
    "performance_video": {
        "phase": "release",
        "words": ["performance video", "performance film"],
    },
    "music_show_stage": {
        "phase": "promotion",
        "words": [
            "inkigayo",
            "music bank",
            "musicbank",
            "m countdown",
            "mcountdown",
            "show! music core",
            "music core",
            "musiccore",
            "show champion",
            "the show",
            "comeback stage",
        ],
    },
    "relay_or_studio_performance": {
        "phase": "promotion",
        "words": ["relay dance", "studio choom", "it's live"],
    },
    "dance_practice": {
        "phase": "choreography",
        "words": ["dance practice", "choreography video", "choreography"],
    },
    "behind_the_scenes": {
        "phase": "era_behind",
        "words": [
            "behind the scenes",
            "behind-the-scenes",
            "mv behind",
            "recording behind",
            "jacket behind",
            "jacket shooting",
            "making film",
            "making-of",
            "dance practice behind",
        ],
    },
}

PHASE_ORDER = [
    "pre_release",
    "release",
    "promotion",
    "choreography",
    "era_behind",
]


CLASSIFICATION_ORDER = [
    "behind_the_scenes",
    "mv_teaser",
    "highlight_medley",
    "concept_or_trailer",
    "official_mv",
    "track_or_live_clip",
    "performance_video",
    "music_show_stage",
    "relay_or_studio_performance",
    "dance_practice",
]

PROMOTIONAL_CHANNEL_WORDS = [
    "smtown",
    "hybe labels",
    "bangtantv",
    "jyp entertainment",
    "yg entertainment",
    "starshiptv",
    "cube entertainment",
    "pledis entertainment",
    "kq entertainment",
    "rbw",
    "wakeone",
    "stone music entertainment",
    "1thek",
    "mnet k-pop",
    "m2",
    "studio choom",
    "kbs kpop",
    "sbs kpop",
    "sbskpop",
    "mbckpop",
    "it's live",
    "hello82",
]

NOISE_PATTERN = re.compile(
    r"\blyrics?\b|color coded|line distribution|reaction|slowed|sped up|nightcore|"
    r"karaoke|instrumental|loop|fancam|fullcam|facecam|focus cam|member cam|직캠|입덕직캠",
    re.I,
)

PUNCTUATION = re.compile(r"[^\w\s]")


def _normalized(value: str) -> str:
    return " ".join(PUNCTUATION.sub(" ", value.casefold()).split())


def _contains_phrase(text: str, phrase: str) -> bool:
    normalized_phrase = _normalized(phrase)
    return bool(normalized_phrase) and normalized_phrase in _normalized(text)


def _source_kind(channel_title: str, artist: str) -> str:
    channel = _normalized(channel_title)

    if _contains_phrase(channel_title, artist):
        return "artist_or_group"

    if any(_normalized(word) in channel for word in PROMOTIONAL_CHANNEL_WORDS):
        return "official_or_promotional"

    if any(word in channel for word in ("official", "entertainment", "records", "labels")):
        return "label_or_official"

    return "other"


def classify_video(video: dict, artist: str, release_title: str, anchor_song: str | None = None,) -> dict | None:
    title = str(video.get("title") or "")
    channel_title = str(video.get("channel_title") or "")

    if not title or NOISE_PATTERN.search(title):
        return None

    if not _contains_phrase(title, artist):
        return None

    release_match = _contains_phrase(title, release_title)
    anchor_match = bool(
        anchor_song
        and _contains_phrase(title, anchor_song)
    )

    if not release_match and not anchor_match:
        return None

    content_type = None
    phase = None
    matched_signal = None

    normalized_title = _normalized(title)

    for rule_name in CLASSIFICATION_ORDER:
        rule = CONTENT_RULES[rule_name]

        for word in rule["words"]:
            if _normalized(word) in normalized_title:
                content_type = rule_name
                phase = rule["phase"]
                matched_signal = word
                break

        if content_type:
            break

    source_kind = _source_kind(channel_title, artist)

    if source_kind == "other":
        return None

    if content_type is None:
        if anchor_match and source_kind == "artist_or_group":
            content_type = "anchor_song_feature"
            phase = "release"
            matched_signal = "official artist-channel anchor song feature"
        else:
            return None

    return {
        **video,
        "phase": phase,
        "content_type": content_type,
        "source_kind": source_kind,
        "matched_signal": matched_signal,
        "matches_release": release_match,
        "matches_anchor_song": anchor_match,
    }


def build_trail(videos: list[dict], artist: str, release_title: str, anchor_song: str | None = None,) -> dict:
    """Classify, deduplicate and order videos into one K-pop comeback trail."""
    classified = []
    seen_video_ids = set()

    for video in videos:
        if not isinstance(video, dict):
            continue

        video_id = str(video.get("video_id") or "")
        if not video_id or video_id in seen_video_ids:
            continue

        item = classify_video(video, artist, release_title, anchor_song)
        if item is None:
            continue

        seen_video_ids.add(video_id)
        classified.append(item)

    phase_rank = {phase: i for i, phase in enumerate(PHASE_ORDER)}
    classified.sort(
        key=lambda video: (
            phase_rank.get(video["phase"], len(PHASE_ORDER)),
            str(video.get("published_at") or ""),
        )
    )

    phases = {phase: [] for phase in PHASE_ORDER}
    for video in classified:
        phases[video["phase"]].append(video)

    missing_phases = [
        phase
        for phase in PHASE_ORDER
        if not phases[phase]
    ]

    return {
        "videos": classified,
        "phases": phases,
        "matched_count": len(classified),
        "missing_phases": missing_phases,
    }


def _failure(code: str, message: str, **extra) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": code,
            "message": message,
            "source": SOURCE,
            **extra,
        },
        ensure_ascii=False,
    )


# --- The tool ---


def trace_kpop_comeback_era(
    artist: str,
    release_title: str,
    anchor_song: str | None = None,
) -> str:
    """Reconstruct one K-pop comeback era from public YouTube content."""
    artist = artist.strip() if isinstance(artist, str) else ""
    release_title = release_title.strip() if isinstance(release_title, str) else ""
    anchor_song = (
        anchor_song.strip()
        if isinstance(anchor_song, str) and anchor_song.strip()
        else None
    )

    if len(artist) < MIN_ARTIST_CHARS:
        return _failure(
            "bad_arguments",
            "Artist name is too short. Give the K-pop artist or group name, e.g. 'aespa'.",
        )

    if len(release_title) < MIN_RELEASE_CHARS:
        return _failure(
            "bad_arguments",
            "Release title is required, e.g. 'Drama' or 'Armageddon'.",
        )

    search_queries = [
    f"{artist} {release_title}",
    f"{artist} {release_title} teaser",
    f"{artist} {release_title} music show",
    f"{artist} {release_title} dance practice",
    f"{artist} {release_title} behind",
    ]

    if (
        anchor_song
        and _normalized(anchor_song) != _normalized(release_title)
    ):
        search_queries.append(
            f"{artist} {anchor_song} track video"
        )

    raw_videos = []

    try:
        client = youtube.get_video_client()

        for query in search_queries:
            raw_videos.extend(
                client.search_videos(
                    query,
                    SEARCH_RESULTS,
                )
            )

    except youtube.YouTubeError as exc:
        return _failure(exc.code, exc.message)

    deduped = []
    seen_video_ids = set()

    for video in raw_videos:
        video_id = str(video.get("video_id") or "")

        if not video_id or video_id in seen_video_ids:
            continue

        seen_video_ids.add(video_id)
        deduped.append(video)

    if not deduped:
        return _failure(
            "no_results",
            f"No YouTube videos were found for '{artist} {release_title}'. "
            "Retry with the official English artist and release names.",
        )

    trail = build_trail(
        deduped,
        artist,
        release_title,
        anchor_song,
    )

    if trail["matched_count"] == 0:
        return _failure(
            "no_results",
            f"YouTube returned videos for '{artist} {release_title}', but none matched "
            "the supported K-pop comeback content types from recognized official or "
            "promotional channels.",
            scanned_count=len(deduped),
        )

    return json.dumps(
        {
            "ok": True,
            "source": SOURCE,
            "artist": artist,
            "release_title": release_title,
            "anchor_song": anchor_song,
            "search_queries": search_queries,
            "scanned_count": len(deduped),
            "matched_count": trail["matched_count"],
            "phases": trail["phases"],
            "missing_phases": trail["missing_phases"],
            "note": (
                "This reconstructs a K-pop comeback trail from YouTube search results. "
                "A missing phase means no matching video was found in the searched "
                "results, not that the content does not exist."
            ),
        },
        ensure_ascii=False,
    )


SCHEMA = {
    "type": "function",
    "function": {
        "name": "trace_kpop_comeback_era",
        "description": (
            "Reconstruct the YouTube content trail around one K-pop comeback era. "
            "It filters unrelated, fan-uploaded and lyric/fancam results, then "
            "classifies surviving official or promotional videos into K-pop-specific "
            "phases: pre-release teasers and album samplers, release-day MV or "
            "performance content, music-show and studio promotions, choreography "
            "content, and behind-the-scenes material. Use it when the user wants "
            "videos or content from a particular K-pop release era, including when "
            "they discovered the era through one song. If anchor_song is supplied, "
            "videos specifically about that song are included alongside release-era "
            "content. Do not use it as a general YouTube search or for non-K-pop "
            "video recommendations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artist": {
                    "type": "string",
                    "description": (
                        "K-pop artist or group name, e.g. 'aespa', 'IVE', "
                        "or 'SEVENTEEN'."
                    ),
                },
                "release_title": {
                    "type": "string",
                    "description": (
                        "The album, EP or single release whose comeback era should "
                        "be reconstructed, e.g. 'Drama' or 'Armageddon'."
                    ),
                },
                "anchor_song": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional song that led the user to this era, especially a "
                        "B-side. When supplied, song-specific Track Videos, live clips "
                        "or performances may be included alongside the wider release "
                        "trail."
                    ),
                },
            },
            "required": ["artist", "release_title"],
        },
    },
}


HANDLER = trace_kpop_comeback_era