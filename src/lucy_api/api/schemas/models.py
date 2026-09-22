"""The model catalogue, sorted by what a person can do with it right now."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SetupResource(BaseModel):
    """What it takes to make a provider usable. Every field is a sentence or a command."""

    command: str = Field(
        description="The `lucy` command that supplies what is missing. Empty when nothing can."
    )
    console_url: str = Field(description="Where a key is created, when one is needed.")
    setting: str = Field(description="The hub setting the value lands in.")
    instructions: str = Field(description="One paragraph a person can follow.")


class StandingResource(BaseModel):
    """One provider, in one of three sections, with the evidence for putting it there."""

    provider: str = Field(description="The id used before the colon in a model spec.")
    title: str
    section: str = Field(description="`ready`, `available` or `unavailable`.")
    dialect: str = Field(description="Which wire format the hub speaks to it.")
    models: list[str] = Field(description="Current flagship ids, spelled as the API wants them.")
    detail: str = Field(description="Why it is in this section, as checked.")
    local: bool = Field(
        description="Whether it is a runtime on this machine rather than a service."
    )
    note: str = Field(description="A deviation worth knowing before choosing it.")
    setup: SetupResource | None = Field(
        default=None, description="Present when something is needed before it can be used."
    )


class ModelsResource(BaseModel):
    """Every provider the hub knows, in the section the last check put it in."""

    ready: list[StandingResource] = Field(
        description="Configured, and a listing call answered. Usable now."
    )
    available: list[StandingResource] = Field(
        description="Configured but not proven: no listing endpoint, or not checked yet."
    )
    unavailable: list[StandingResource] = Field(
        description="Not configured, refused, or unusable -- each with what would fix it."
    )
