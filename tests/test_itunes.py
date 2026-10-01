"""Tests for the shared iTunes song-to-release resolver."""

import json

import tools.common.itunes as itunes_module
from tools import TOOL_MAP, TOOLS, run_tool


def _result(
    track_name,
    artist_name,
    collection_name,
    collection_id,
    track_id,
    release_date,
    track_count,
):
    return {
        "trackName": track_name,
        "artistName": artist_name,
        "collectionName": collection_name,
        "collectionId": collection_id,
        "trackId": track_id,
        "releaseDate": release_date,
        "trackCount": track_count,
        "primaryGenreName": "K-Pop",
        "trackViewUrl": "https://music.apple.com/track",
        "collectionViewUrl": "https://music.apple.com/album",
    }


def test_tool_is_registered():
    assert "resolve_song_release" in TOOL_MAP
    assert "resolve_song_release" in [
        tool["function"]["name"]
        for tool in TOOLS
    ]


def test_release_title_removes_album_suffix():
    data = {
        "results": [
            _result(
                "Licorice",
                "aespa",
                "Armageddon - The 1st Album",
                1,
                2,
                "2024-05-27T12:00:00Z",
                10,
            )
        ]
    }

    matches = itunes_module.resolve_song_results(
        data,
        "aespa",
        "Licorice",
    )

    assert matches[0]["release_title"] == "Armageddon"


def test_derivative_release_is_marked():
    data = {
        "results": [
            _result(
                "Smart",
                "LE SSERAFIM",
                "Smart (Remixes)",
                1,
                2,
                "2024-03-01T12:00:00Z",
                5,
            )
        ]
    }

    matches = itunes_module.resolve_song_results(
        data,
        "LE SSERAFIM",
        "Smart",
    )

    assert matches[0]["is_derivative_release"] is True


def test_original_ep_beats_remix_release(monkeypatch):
    data = {
        "results": [
            _result(
                "Smart",
                "LE SSERAFIM",
                "EASY - EP",
                100,
                101,
                "2024-02-19T12:00:00Z",
                5,
            ),
            _result(
                "Smart",
                "LE SSERAFIM",
                "Smart (Remixes)",
                200,
                201,
                "2024-02-19T12:00:00Z",
                5,
            ),
        ]
    }

    class FakeClient:
        def search_songs(self, artist, song, country):
            return data

    monkeypatch.setattr(
        itunes_module,
        "get_song_client",
        lambda: FakeClient(),
    )

    result = json.loads(
        run_tool(
            "resolve_song_release",
            {
                "artist": "LE SSERAFIM",
                "song": "Smart",
            },
        )
    )

    assert result["ok"] is True
    assert result["release_title"] == "EASY"
    assert result["collection_name"] == "EASY - EP"
    assert result["is_derivative_release"] is False


def test_no_exact_song_match_returns_no_results(monkeypatch):
    class FakeClient:
        def search_songs(self, artist, song, country):
            return {
                "results": [
                    _result(
                        "Different Song",
                        artist,
                        "Some Album",
                        1,
                        2,
                        "2024-01-01T12:00:00Z",
                        10,
                    )
                ]
            }

    monkeypatch.setattr(
        itunes_module,
        "get_song_client",
        lambda: FakeClient(),
    )

    result = json.loads(
        run_tool(
            "resolve_song_release",
            {
                "artist": "aespa",
                "song": "Licorice",
            },
        )
    )

    assert result["ok"] is False
    assert result["error"] == "no_results"