"""Update Parsers Test Fixtures module
Using Blizzard real data about heroes, some players and maps,
download and update parsers test HTML fixtures
"""

import argparse
import asyncio
from pathlib import Path

import httpx2
from fastapi import status

from app.config import settings
from app.domain.enums import HeroKey, Locale
from app.infrastructure.logger import logger
from tests.helpers import players_ids, unknown_player_id


def parse_parameters() -> argparse.Namespace:  # pragma: no cover
    """Parse command line arguments and returns the corresponding Namespace object"""
    parser = argparse.ArgumentParser(
        description=(
            "Update test data fixtures by retrieving Blizzard pages directly. "
            "By default, all the tests data will be updated."
        ),
    )
    parser.add_argument(
        "-He",
        "--heroes",
        action="store_true",
        default=False,
        help="update heroes test data",
    )
    parser.add_argument(
        "-Ho",
        "--home",
        action="store_true",
        default=False,
        help="update home test data (roles, gamemodes)",
    )
    parser.add_argument(
        "-P",
        "--players",
        action="store_true",
        default=False,
        help="update players test data",
    )

    parser.add_argument(
        "-Pn",
        "--patch-notes",
        action="store_true",
        default=False,
        help="update patch notes test data",
    )

    args = parser.parse_args()

    # If no value was given by the user, all is true
    if not any(vars(args).values()):
        args.heroes = True
        args.home = True
        args.players = True
        args.patch_notes = True

    return args


def list_routes_to_update(args: argparse.Namespace) -> dict[tuple[Locale, str], str]:
    """Method used to construct the dict of routes to update. The result
    is a dictionnary, mapping a (locale, blizzard route path) pair to the
    local filepath."""
    english = Locale.ENGLISH_US
    route_file_mapping: dict[tuple[Locale, str], str] = {}

    if args.heroes:
        logger.info("Adding heroes routes...")

        route_file_mapping |= {
            (english, f"{settings.heroes_path}"): "/heroes.html",
            **{
                (english, f"{settings.heroes_path}{hero}/"): f"/heroes/{hero}.html"
                for hero in HeroKey
            },
        }

    if args.players:
        logger.info("Adding player careers routes...")
        # career_path already includes the "/en-us" locale prefix, which main()
        # also prepends to every route, so strip it here to avoid duplication.
        career_path = settings.career_path.removeprefix(f"/{english}")
        route_file_mapping |= {
            (english, f"{career_path}/{player_id}/"): f"/players/{player_id}.html"
            for player_id in [*players_ids, unknown_player_id]
        }

    if args.home:
        logger.info("Adding home routes...")
        route_file_mapping[(english, settings.home_path)] = "/home.html"

    if args.patch_notes:
        logger.info("Adding patch notes route...")
        route_file_mapping[(english, settings.patch_notes_path)] = "/patch-notes.html"

        # Hero names in the patch notes are localised and heroes.csv is not, so
        # the localised heroes list is what resolves them. The two fr-fr pages
        # only make sense as a pair — refresh them together or the locale tests
        # compare a new patch note against an old hero roster.
        logger.info("Adding localised patch notes routes...")
        route_file_mapping |= {
            (Locale.FRENCH, settings.patch_notes_path): "/patch-notes-fr-fr.html",
            (Locale.FRENCH, settings.heroes_path): "/heroes-fr-fr.html",
        }

    return route_file_mapping


def save_fixture_file(filepath: str, content: str):  # pragma: no cover
    """Method used to save the fixture file on the disk"""
    with Path(filepath).open(mode="w", encoding="utf-8") as html_file:
        html_file.write(content)
        html_file.close()
        logger.info("File saved !")


async def main():
    """Main method of the script"""
    logger.info("Updating test fixtures...")

    args = parse_parameters()
    logger.debug("args : {}", args)

    # Initialize data
    route_file_mapping = list_routes_to_update(args)

    # Do the job
    test_data_path = f"{settings.test_fixtures_root_path}/html"
    async with httpx2.AsyncClient() as client:
        for (locale, route), filepath in route_file_mapping.items():
            logger.info("Updating {}{}...", test_data_path, filepath)
            logger.info("GET {}/{}{}...", settings.blizzard_host, locale, route)
            response = await client.get(
                f"{settings.blizzard_host}/{locale}{route}",
                headers={"Accept": "text/html"},
                follow_redirects=True,
            )
            logger.debug(
                "HTTP {} / Time : {}",
                response.status_code,
                response.elapsed.total_seconds(),
            )
            if response.status_code in {status.HTTP_200_OK, status.HTTP_404_NOT_FOUND}:
                save_fixture_file(f"{test_data_path}{filepath}", response.text)
            else:
                logger.error("Error while getting the page : {}", response.text)

    logger.info("Fixtures update finished !")


if __name__ == "__main__":  # pragma: no cover
    logger = logger.patch(lambda record: record.update(name="update_test_fixtures"))
    asyncio.run(main())
