"""E2E test fixtures with vcrpy recording/replay."""
import os
import pytest
from pathlib import Path

CASSETTE_DIR = Path(__file__).parent / "cassettes"

# ---- vcrpy configuration ----
# Recording mode controlled by VCR_MODE environment variable:
#   - unset (default): "none" — use only existing cassettes, no network
#   - "record": re-record specified cassettes (delete old, record new)
#   - "new_episodes": append new interactions to existing cassettes

VCR_MODE = os.environ.get("VCR_MODE", "none")


@pytest.fixture(scope="module")
def vcr_config():
    """Global vcrpy configuration.

    Filter Authorization headers to prevent API key leaks in cassette files.
    """
    return {
        "filter_headers": ["authorization", "x-api-key", "api-key"],
        "filter_query_parameters": ["key", "api_key"],
        "decode_compressed_response": True,
    }


@pytest.fixture
def real_config():
    """Build an AgentConfig using real API keys from the environment.

    Picks the first available provider.  Respects ``ANTHROPIC_BASE_URL`` /
    ``ANTHROPIC_AUTH_TOKEN`` overrides (e.g. when Anthropic is routed to a
    DeepSeek-compatible endpoint).

    In replay mode (``VCR_MODE=none``): uses a dummy key for whatever provider
    the cassette was recorded against — vcrpy intercepts all HTTP traffic.
    """
    from agent_core import AgentConfig

    # Gather explicit provider keys from standard env vars
    _key_env_map = [
        ("ANTHROPIC_API_KEY", "anthropic"),
        ("DEEPSEEK_API_KEY", "deepseek"),
        ("OPENAI_API_KEY", "openai"),
    ]
    api_keys = {}
    for ev, provider in _key_env_map:
        key = os.environ.get(ev)
        if key:
            api_keys[provider] = key

    # Detect Anthropic-over-DeepSeek routing (ANTHROPIC_BASE_URL pointing to
    # api.deepseek.com + ANTHROPIC_AUTH_TOKEN set to a DeepSeek key).
    anthro_base = os.environ.get("ANTHROPIC_BASE_URL", "")
    anthro_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    deepseek_routed = (
        "deepseek" in anthro_base
        and anthro_token
        and "anthropic" not in api_keys
    )

    # Determine model + base_url based on what keys / routing are actually
    # available, not on a hard-coded default.
    if deepseek_routed:
        # Anthropic SDK → DeepSeek proxy: use anthropic provider name but
        # route through base_url so vcrpy sees the correct host.
        model = "anthropic/claude-haiku-4-5"
        api_keys["anthropic"] = anthro_token
        base_urls = {"anthropic": anthro_base}
    elif "deepseek" in api_keys:
        # Native DeepSeek API key — use DeepSeekClient directly.
        model = "deepseek/deepseek-v4-pro"
        base_urls = {}
    elif "openai" in api_keys:
        model = "openai/gpt-4o"
        base_urls = {}
    elif "anthropic" in api_keys:
        model = "anthropic/claude-haiku-4-5"
        base_urls = {}
    elif VCR_MODE == "none":
        # Replay mode: dummy key is fine — vcrpy intercepts the request.
        # The committed cassettes were recorded against the Anthropic SDK routed
        # through DeepSeek's anthropic-compatible proxy
        # (https://api.deepseek.com/anthropic/v1/messages).  We MUST reproduce
        # that base_url here, otherwise the replayed request would target
        # api.anthropic.com and never match the cassette under match_on=uri.
        model = "anthropic/claude-haiku-4-5"
        api_keys = {"anthropic": "sk-dummy-replay-key"}
        base_urls = {"anthropic": "https://api.deepseek.com/anthropic"}
    else:
        pytest.skip(
            "No API key configured "
            "(set ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, or OPENAI_API_KEY)"
        )

    return AgentConfig(
        model=model,
        max_turns=5,
        api_keys=api_keys,
        base_urls=base_urls,
        permission_mode="bypassPermissions",
    )


@pytest.fixture
def vcr(request, vcr_config):
    """One cassette per test function, named after the test function.

    In replay mode (VCR_MODE=none), skips if cassette is missing so that
    unrecorded tests give a clear skip rather than a cryptic error.
    """
    import vcr as vcrpy_lib

    cassette_name = f"{request.node.name}.yaml"
    cassette_path = CASSETTE_DIR / cassette_name

    # Replay mode with missing cassette → explicit skip
    # (first run needs VCR_MODE=record)
    if VCR_MODE == "none" and not cassette_path.exists():
        pytest.skip(
            f"cassette not recorded: {cassette_name} "
            "(run with VCR_MODE=record and an API key to record)"
        )

    my_vcr = vcrpy_lib.VCR(
        record_mode=VCR_MODE,
        cassette_library_dir=str(CASSETTE_DIR),
        match_on=["method", "uri"],
        **vcr_config,
    )
    with my_vcr.use_cassette(cassette_name):
        yield
