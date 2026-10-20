"""Ask Dexra whether a fresh pump.fun coin is worth buying, before you buy it.

Usage:
    uv run cookbook/pumpfun/read/pumpfun_check_token_dexra.py <MINT> [<MINT> ...]
    uv run cookbook/pumpfun/read/pumpfun_check_token_dexra.py <MINT> --refuse avoid caution

Sends the mints to Dexra's `new_launches/batch` (https://dexra.dev/docs) and
prints one verdict per coin — `ok`, `caution`, `avoid` or `unknown` — with the
reasons behind it, worst first, and the facts they rest on: the creator's
earlier launches and how many collapsed, whether the first buyers bought in the
launch block or share a funder, and known sniper wallets among them.

Needs `DEXRA_API_KEY` in `.env`, a key from the Dexra cabinet. A free key checks
up to 5 mints a call within its daily credits; past them the call answers HTTP
402 and the script says so. It moves no funds.

The creator's history is there from the first second. The first buyers are
analyzed when you ask, so the answer on a coin seconds old can take a while; if
it still says "first buyers are not analyzed yet", `--wait` asks again until
they are or the time runs out.

The exit status is 1 when any coin's verdict is listed under `--refuse`
(default `avoid`) or a coin is not found, so a shell loop can gate a buy on it.
"""

import argparse
import asyncio
import os
import sys
from http import HTTPStatus
from typing import Any, Final

import aiohttp
from dotenv import load_dotenv

DEFAULT_ENDPOINT: Final[str] = "https://api.dexra.dev"
# Dexra analyzes a coin's first buyers on demand for up to 25 s before it answers.
DEFAULT_TIMEOUT: Final[float] = 30.0
DEFAULT_WAIT: Final[float] = 0.0
DEFAULT_REFUSE: Final[list[str]] = ["avoid"]
POLL_INTERVAL: Final[float] = 3.0
VERDICTS: Final[list[str]] = ["ok", "caution", "avoid", "unknown"]

load_dotenv()

API_KEY = os.environ.get("DEXRA_API_KEY")


# What a refusal means for the caller, by HTTP status.
REFUSALS: Final[dict[int, str]] = {
    HTTPStatus.UNAUTHORIZED: "DEXRA_API_KEY is invalid or revoked",
    HTTPStatus.PAYMENT_REQUIRED: (
        "today's credits ran out, or the key's plan takes fewer mints "
        "per call (see https://dexra.dev/#pricing)"
    ),
    HTTPStatus.TOO_MANY_REQUESTS: "rate limited; slow down or raise the plan's limit",
}


class DexraError(Exception):
    """Dexra refused the call; the message says why and what to do."""

    def __init__(self, status: int, body: object) -> None:
        super().__init__(REFUSALS.get(status, f"HTTP {status}: {body}"))


async def check(
    session: aiohttp.ClientSession, endpoint: str, mints: list[str]
) -> list[dict[str, Any]]:
    """Return one batch result per mint, in the order asked."""
    async with session.post(
        f"{endpoint}/new_launches/batch", json={"mints": mints}
    ) as response:
        body = await response.json(content_type=None)
        if response.status != HTTPStatus.OK:
            raise DexraError(response.status, body)
    return body["result"]["results"]


def analysis_pending(result: dict[str, Any]) -> bool:
    """True while a found coin's first buyers have not been analyzed yet."""
    return any(
        launch["first_buyers"]["status"] == "pending" for launch in result["launches"]
    )


def report(result: dict[str, Any]) -> None:
    """Print one coin's verdict, reasons and the facts behind them."""
    print(f"\n{result['mint']}")
    if not result["found"]:
        print("  not found: not a launch Dexra tracks, or created moments ago")
        return
    # One mint can have several pools, e.g. the pump.fun curve and its PumpSwap pool.
    for launch in result["launches"]:
        liquidity = launch["liquidity"]
        creator = launch["creator"]
        buyers = launch["first_buyers"]
        print(
            f"  {launch['platform']} · {launch['age_secs']}s old · "
            f"verdict: {launch['verdict'].upper()}"
        )
        for reason in launch["reasons"]:
            print(f"    - {reason}")
        reserve = liquidity["quote_reserve"]
        if reserve is not None:
            print(
                f"  liquidity: {reserve:.2f} {liquidity['quote_symbol']} "
                f"(peak {liquidity['peak_quote_reserve']:.2f}), lock {liquidity['lock']}"
            )
        print(
            f"  creator {creator['address']}: {creator['previous_launches']} earlier "
            f"launches, {creator['collapsed_launches']} collapsed, "
            f"{creator['graduated_launches']} graduated"
        )
        if buyers["status"] == "pending":
            print("  first buyers: not analyzed yet")
        else:
            supply = buyers["first_buyers_supply_pct"]
            held = f", holding {supply:.1f}% of supply" if supply is not None else ""
            print(
                f"  first buyers: {buyers['analyzed_wallets']} analyzed, "
                f"{buyers['same_block']} in the launch block, "
                f"{buyers['shared_funder_wallets']} sharing a funder, "
                f"{buyers['known_snipers']} known snipers{held}"
            )


async def run(mints: list[str], endpoint: str, timeout: float, wait: float) -> list:
    headers = {"Authorization": f"Bearer {API_KEY}"}
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(
        headers=headers, timeout=client_timeout
    ) as session:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        results = await check(session, endpoint, mints)
        while any(map(analysis_pending, results)) and loop.time() < deadline:
            await asyncio.sleep(min(POLL_INTERVAL, deadline - loop.time()))
            results = await check(session, endpoint, mints)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ask Dexra for a verdict on fresh pump.fun coins before buying."
    )
    parser.add_argument("mints", nargs="+", metavar="MINT", help="coin mint address")
    parser.add_argument(
        "--refuse",
        nargs="+",
        choices=VERDICTS,
        default=DEFAULT_REFUSE,
        help="verdicts that make the exit status 1 (default: %(default)s)",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_WAIT,
        help="seconds to keep asking while first buyers are not analyzed yet "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="seconds one request may take (default: %(default)s)",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help="Dexra API base URL (default: %(default)s)",
    )
    args = parser.parse_args()

    if not API_KEY:
        sys.exit("Set DEXRA_API_KEY in .env (create a key in the Dexra cabinet)")

    try:
        results = asyncio.run(run(args.mints, args.endpoint, args.timeout, args.wait))
    except DexraError as error:
        sys.exit(f"Dexra: {error}")
    except (TimeoutError, aiohttp.ClientError) as error:
        sys.exit(f"Dexra did not answer: {error!r}")

    refused = False
    for result in results:
        report(result)
        verdicts = {launch["verdict"] for launch in result["launches"]}
        if not result["found"] or verdicts & set(args.refuse):
            refused = True
    sys.exit(1 if refused else 0)


if __name__ == "__main__":
    main()
