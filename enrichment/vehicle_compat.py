"""Sprint 1 — Motor de compatibilidad vehículo↔spot (lógica pura, sin DB/LLM).

Diseño paramétrico: el usuario configura las MEDIDAS reales de su vehículo + tracción.
El veredicto se calcula comparando esas medidas contra las restricciones del spot
(tabla `spot_vehicle_access`). No hay clases fijas; los presets son solo atajos.

Principios (ver docs/auditoria-compatibilidad-vehiculos.md §7):
  - 3 estados: APTO / NO_APTO / DESCONOCIDO. Nunca un score 0-100.
  - NULL/ausencia = DESCONOCIDO, jamás NO_APTO. "No etiquetado" no es "no apto".
  - Asimetría de error: un falso "es accesible" para un vehículo grande es catastrófico.
    Por eso DESCONOCIDO se trata distinto según el tamaño del vehículo (conservative).
  - Una camper pequeña (≤5 m × ≤2 m, sin necesidad de 4x4) "entra en casi todo": para ella
    un DESCONOCIDO en una dimensión se resuelve a APTO. Para una AC grande, a PRECAUCIÓN.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── Conocimiento de dominio (umbrales aportados por el usuario, 2026-05-30) ──────────────
# Camper pequeña: hasta ~5 m largo y ~2 m alto → entra en todos lados.
# Gran volumen: 5–6,5 m largo, ~2,6–2,9 m alto.
# Autocaravana: enorme variedad, desde gran-volumen hasta gigantes tipo Concorde (~9 m / 3,5 m).
# Por debajo de estos umbrales, un DESCONOCIDO en esa dimensión se considera seguro (APTO):
SAFE_UNKNOWN_LENGTH_M = 5.5    # ≤ esto: un límite de longitud desconocido casi nunca excluye
SAFE_UNKNOWN_HEIGHT_M = 2.2    # ≤ esto: un límite de altura desconocido casi nunca excluye
SAFE_UNKNOWN_WIDTH_M = 2.2

# Superficies que penalizan a vehículos sin tracción.
ROUGH_SURFACES = {"dirt", "sand", "grass"}


class State(str, Enum):
    APTO = "apto"
    NO_APTO = "no_apto"
    DESCONOCIDO = "desconocido"
    PRECAUCION = "precaucion"   # solo en el veredicto conservador: desconocido que conviene avisar


@dataclass(frozen=True)
class VehicleProfile:
    """Medidas reales del vehículo del usuario. Altura/ancho incluyen extras de techo."""
    length_m: float
    height_m: float
    width_m: float = 2.3
    weight_t: Optional[float] = None
    has_4wd: bool = False

    @property
    def is_small(self) -> bool:
        """Camper pequeña que 'entra en todos lados'."""
        return self.length_m <= SAFE_UNKNOWN_LENGTH_M and self.height_m <= SAFE_UNKNOWN_HEIGHT_M


# Presets de conveniencia (el usuario puede no medir; son puntos de partida editables).
VEHICLE_PRESETS: dict[str, VehicleProfile] = {
    "camper_pequena":  VehicleProfile(length_m=5.0, height_m=2.0, width_m=2.0),
    "gran_volumen":    VehicleProfile(length_m=6.0, height_m=2.7, width_m=2.1),
    "ac_perfilada":    VehicleProfile(length_m=7.0, height_m=2.9, width_m=2.3),
    "ac_capuchina":    VehicleProfile(length_m=7.0, height_m=3.2, width_m=2.3),
    "ac_grande":       VehicleProfile(length_m=8.5, height_m=3.4, width_m=2.5, weight_t=4.5),
    "4x4_camper":      VehicleProfile(length_m=5.5, height_m=2.8, width_m=2.1, has_4wd=True),
}


@dataclass
class SpotConstraints:
    """Restricciones del spot (refleja spot_vehicle_access). NULL/None = desconocido."""
    max_length_m: Optional[float] = None
    max_height_m: Optional[float] = None
    max_width_m: Optional[float] = None
    max_weight_t: Optional[float] = None
    requires_4wd: Optional[bool] = None
    steep_access: Optional[bool] = None
    surface: Optional[str] = None
    field_confidence: dict = field(default_factory=dict)


@dataclass
class DimensionResult:
    dimension: str
    state: State
    reason: str
    confidence: float = 0.0


@dataclass
class CompatVerdict:
    """Resultado del matching. `strict` no perdona desconocidos; `conservative` los avisa
    según el tamaño del vehículo (modo recomendado para AC grandes)."""
    strict: State
    conservative: State
    dimensions: list[DimensionResult]
    reasons: list[str]

    @property
    def is_hard_excluded(self) -> bool:
        return self.strict == State.NO_APTO


def _eval_numeric(dim: str, spot_max: Optional[float], vehicle_val: float,
                  safe_unknown_threshold: float, conf: float) -> DimensionResult:
    """Evalúa una dimensión numérica (largo/alto/ancho/peso)."""
    if spot_max is None:
        # Desconocido. Para vehículo pequeño en esa dimensión, asumir seguro.
        if vehicle_val <= safe_unknown_threshold:
            return DimensionResult(dim, State.APTO,
                                   f"{dim}: límite desconocido, pero {vehicle_val} m es pequeño → seguro", 0.5)
        return DimensionResult(dim, State.DESCONOCIDO,
                               f"{dim}: límite del spot desconocido y vehículo grande ({vehicle_val} m)", 0.0)
    if vehicle_val > spot_max:
        return DimensionResult(dim, State.NO_APTO,
                               f"{dim}: vehículo {vehicle_val} m > límite spot {spot_max} m", conf)
    margin = spot_max - vehicle_val
    return DimensionResult(dim, State.APTO,
                           f"{dim}: cabe ({vehicle_val} m ≤ {spot_max} m, margen {margin:.1f} m)", conf)


def evaluate(spot: SpotConstraints, vehicle: VehicleProfile) -> CompatVerdict:
    """Calcula el veredicto de compatibilidad física para un vehículo en un spot."""
    dims: list[DimensionResult] = []
    fc = spot.field_confidence or {}

    dims.append(_eval_numeric("longitud", spot.max_length_m, vehicle.length_m,
                              SAFE_UNKNOWN_LENGTH_M, fc.get("max_length_m", 0.7)))
    dims.append(_eval_numeric("altura", spot.max_height_m, vehicle.height_m,
                              SAFE_UNKNOWN_HEIGHT_M, fc.get("max_height_m", 0.7)))
    dims.append(_eval_numeric("anchura", spot.max_width_m, vehicle.width_m,
                              SAFE_UNKNOWN_WIDTH_M, fc.get("max_width_m", 0.6)))
    if vehicle.weight_t is not None:
        dims.append(_eval_numeric("peso", spot.max_weight_t, vehicle.weight_t,
                                  99.0, fc.get("max_weight_t", 0.6)))

    # Tracción / terreno.
    if spot.requires_4wd is True:
        if vehicle.has_4wd:
            dims.append(DimensionResult("traccion", State.APTO, "requiere 4x4 y el vehículo lo tiene",
                                        fc.get("requires_4wd", 0.6)))
        else:
            dims.append(DimensionResult("traccion", State.NO_APTO, "acceso solo 4x4 y el vehículo no tiene tracción",
                                        fc.get("requires_4wd", 0.6)))
    elif spot.requires_4wd is None and spot.surface in ROUGH_SURFACES and not vehicle.has_4wd:
        # Superficie rugosa sin tracción → desconocido/precaución, nunca exclusión dura.
        state = State.APTO if vehicle.is_small else State.DESCONOCIDO
        dims.append(DimensionResult("terreno", state,
                                    f"superficie '{spot.surface}' sin 4x4; "
                                    + ("vehículo pequeño, asumible" if vehicle.is_small else "precaución vehículo grande"),
                                    0.3))

    # Pendiente fuerte conocida → precaución para vehículos largos/pesados (no exclusión dura).
    if spot.steep_access is True and not vehicle.is_small:
        dims.append(DimensionResult("pendiente", State.DESCONOCIDO,
                                    "aproximación con pendiente fuerte; precaución vehículo grande", 0.4))

    # ── Agregación ──────────────────────────────────────────────────────────────────────
    states = {d.state for d in dims}
    reasons = [d.reason for d in dims if d.state in (State.NO_APTO, State.DESCONOCIDO)]

    if State.NO_APTO in states:
        strict = State.NO_APTO          # cualquier exclusión dura gana (asimetría de seguridad)
    elif State.DESCONOCIDO in states:
        strict = State.DESCONOCIDO
    else:
        strict = State.APTO

    # Conservador: un DESCONOCIDO solo "preocupa" si el vehículo es grande.
    if strict == State.DESCONOCIDO:
        conservative = State.PRECAUCION if not vehicle.is_small else State.APTO
    else:
        conservative = strict

    if not reasons:
        reasons = ["todas las dimensiones conocidas son compatibles"]
    return CompatVerdict(strict=strict, conservative=conservative, dimensions=dims, reasons=reasons)


# ── Preferencias del usuario (ranking, NO exclusión) ─────────────────────────────────────
# La compatibilidad física (arriba) decide qué spots son SEGUROS. Las preferencias deciden
# cuáles gustan MÁS, y se aplican como ranking sobre los que ya pasan el filtro físico.
# No reinventan nada: mapean a columnas/señales que ya produce el pipeline semántico
# (spot_semantic_state). El ranking en sí se implementa en la API (Sprint 4).
#
# Mapa preferencia → señal existente:
#   FÍSICAS DEL SPOT (el aparcamiento en sí)
#     suelo_llano        → (Sprint 6 geo: pendiente del propio spot)
#     tranquilo          → quietness_score
#     discreto/stealth   → stealth_score
#     seguro_noche       → overnight_safe / safety_score
#     poco_concurrido    → crowd_level_score (invertido)
#     sombra             → signals_data.shade_morning / shade_afternoon
#   DEL ENTORNO
#     bonito             → beauty_score
#     vista_mar          → signals_data.sea_view
#     vista_montana      → signals_data.mountain_view
#     lago_cerca         → signals_data.lake_nearby
#     naturaleza         → tipo IN (wild, naturaleza) + beauty_score
#     lejos_carretera    → road_noise (invertido)

# Preferencias declaradas como (clave → peso 0..1). Peso 0 = indiferente.
PREFERENCE_SIGNALS = {
    "tranquilo": "quietness_score",
    "discreto": "stealth_score",
    "seguro_noche": "overnight_safe",
    "poco_concurrido": "crowd_level_score",      # invertir
    "bonito": "beauty_score",
    "vista_mar": "signals_data.sea_view",
    "vista_montana": "signals_data.mountain_view",
    "lago_cerca": "signals_data.lake_nearby",
    "lejos_carretera": "road_noise",             # invertir
}


@dataclass
class UserProfile:
    """Configuración completa del usuario: vehículo + preferencias.
    El vehículo gobierna la SEGURIDAD (filtro físico); las preferencias, el RANKING."""
    vehicle: VehicleProfile
    conservative_mode: bool = True               # AC grandes: avisar de los desconocidos
    preferences: dict[str, float] = field(default_factory=dict)   # {"tranquilo":1.0,"vista_mar":0.6}
