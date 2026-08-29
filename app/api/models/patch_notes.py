"""Set of pydantic models used for Patch Notes API routes"""

import datetime

from pydantic import BaseModel, Field

from app.domain.enums import HeroKey


class PatchNoteAbility(BaseModel):
    name: str = Field(
        ...,
        description="Name of the ability, as Blizzard labels it in the patch notes",
        examples=["Surging Strike"],
    )
    details: list[str] = Field(
        ...,
        description=(
            "Changes listed under this ability, one entry per bullet point, "
            "in the order Blizzard published them."
        ),
        examples=[["Cast Time reduced from 0.15 to 0.05 seconds."]],
    )


class PatchNoteEntry(BaseModel):
    title: str = Field(
        ...,
        description=(
            "Title of the entry : the hero name for a hero update, the feature "
            "name for a general update, the area name for a map update."
        ),
        examples=["D.Mon"],
    )
    hero: HeroKey | None = Field(
        None,
        description=(
            "Hero key matching the entry title, for hero updates. ``null`` when "
            "the name doesn't resolve : Blizzard names a hero in its patch notes "
            "on release day, before this API knows it, and the names are "
            "localised while the mapping is English-only. The raw name is always "
            "kept in ``title``."
        ),
        examples=["dmon"],
    )
    details: list[str] = Field(
        ...,
        description=(
            "Changes listed for this entry, one entry per bullet point or "
            "paragraph, in the order Blizzard published them. Always empty for "
            "map updates : those are before/after screenshots and carry no text."
        ),
        examples=[["Base movement speed reduced from 6 to 5.5 meters per second."]],
    )
    abilities: list[PatchNoteAbility] = Field(
        ...,
        description=(
            "Per-ability changes, for hero updates. Empty for every other kind "
            "of entry, and for a hero update that only changed general values."
        ),
        examples=[
            [
                {
                    "name": "Surging Strike",
                    "details": ["Cast Time reduced from 0.15 to 0.05 seconds."],
                }
            ]
        ],
    )


class PatchNoteSection(BaseModel):
    title: str | None = Field(
        None,
        description=(
            "Section title as Blizzard wrote it, e.g. 'Hero Updates', 'Bug "
            "Fixes' or 'Busan - Control'. ``null`` on the untitled section a "
            "text-only announcement is published as."
        ),
        examples=["Hero Updates"],
    )
    kind: str = Field(
        ...,
        description=(
            "Blizzard's own section kind : ``hero_update``, ``generic_update`` "
            "or ``map_update``. Passed through as-is, not mapped onto a "
            "taxonomy of this API's making."
        ),
        examples=["hero_update"],
    )
    description: str | None = Field(
        None,
        description=(
            "Introduction text for the section, newline-separated when Blizzard "
            "wrote several paragraphs. ``null`` when there is none."
        ),
        examples=["This is a hotfix update."],
    )
    entries: list[PatchNoteEntry] = Field(
        ...,
        description="Individual updates listed in the section",
    )


class PatchNote(BaseModel):
    date: datetime.date = Field(
        ...,
        description=(
            "Publication date of the patch, as an ISO-8601 date. Read from the "
            "patch anchor, which is identical in every locale."
        ),
        examples=["2026-08-14"],
    )
    title: str = Field(
        ...,
        description="Title of the patch, in the requested locale",
        examples=["Overwatch Retail Patch Notes - August 14, 2026"],
    )
    sections: list[PatchNoteSection] = Field(
        ...,
        description="Sections of the patch, in the order Blizzard published them",
    )
