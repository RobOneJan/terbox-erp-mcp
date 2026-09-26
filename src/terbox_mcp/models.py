"""Pydantic models mirroring the TER BOX CAD Backend OpenAPI schema."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from typing import Literal

WallHeight = Literal["full", "half", "none"]

WallMaterial = Literal[
    "wpc",
    "realWood",
    "glass",
    "meshFence",
    "meshFenceWithPrivacy",
    "corrugatedSheet",
]

WpcColor = Literal["cedar", "darkGrey", "teak", "ipe", "lightGrey"]

FloorMaterial = Literal["wpcFloor", "woodFloor"]

MountingOption = Literal["noMounting", "bolted", "underPaving", "concrete"]

FrameColor = Literal[
    "tiefschwarz",
    "verkehrsweiss",
    "anthrazitgrau",
    "lichtgrau",
    "feuerrot",
    "enzianblau",
    "moosgruen",
    "schokoladenbraun",
]


class BoxConfig(BaseModel):
    width_mm: float
    depth_mm: float
    height_mm: float
    with_roof: bool = True
    with_floor: bool = True
    walls: WallHeight = "full"
    wall_material: Optional[WallMaterial] = None
    wall_wpc_color: Optional[WpcColor] = None
    floor_material: Optional[FloorMaterial] = None
    floor_wpc_color: Optional[WpcColor] = None
    roller_door: bool = False
    roller_door_color: Optional[str] = None
    with_sliding_door: bool = False
    with_single_door: bool = False
    with_double_door: bool = False
    with_solar: bool = False
    with_bike_stand: bool = False
    with_led: bool = False
    n_steckdosen: int = 0
    with_dachbegrunung: bool = False
    with_wechselrichter: bool = False
    n_kameras: int = 0
    mounting: Optional[MountingOption] = None
    frame_color: Optional[FrameColor] = Field(
        default="anthrazitgrau",
        description=(
            "Steel frame color. Applies to all tubes, connectors, roof "
            "substructure and Schienen."
        ),
    )


class KundeRequest(BaseModel):
    anrede: Optional[str] = None
    vorname: str
    nachname: str
    firma: Optional[str] = None
    email: Optional[str] = None
    telefon: Optional[str] = None
    strasse: Optional[str] = None
    plz: Optional[str] = None
    ort: Optional[str] = None
    land: str = "Deutschland"


class KundeResponse(BaseModel):
    contact_id: str
    kundennummer: str
    vorname: str
    nachname: str
    firma: Optional[str] = None


class AngebotPosition(BaseModel):
    beschreibung: str
    menge: float = 1.0
    einheit: str = "Stk"
    einzelpreis: float


class BoxAngebotRequest(BaseModel):
    config: BoxConfig
    sevdesk_contact_id: str
    datum: Optional[str] = None
    extras: list[AngebotPosition] = Field(default_factory=list)
