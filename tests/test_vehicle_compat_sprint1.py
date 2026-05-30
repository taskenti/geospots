"""Sprint 1 — Tests del motor de compatibilidad vehículo↔spot.

Cubre la lógica crítica de seguridad (asimetría de error):
  - Camper pequeña "entra en todos lados" (desconocidos → apto).
  - AC grande con desconocidos → desconocido/precaución (nunca apto a ciegas).
  - Límite conocido superado → NO_APTO duro (gana sobre cualquier desconocido).
  - 4x4 obligatorio sin tracción → NO_APTO; con tracción → apto.
  - Ausencia jamás produce NO_APTO.

Ejecutar:  python -m tests.test_vehicle_compat_sprint1
"""

from enrichment.vehicle_compat import (
    VehicleProfile, SpotConstraints, State, evaluate, VEHICLE_PRESETS,
)

SMALL = VEHICLE_PRESETS["camper_pequena"]      # 5,0 × 2,0
BIG = VEHICLE_PRESETS["ac_grande"]             # 8,5 × 3,4, 4,5 t, sin 4x4
FOURX4 = VEHICLE_PRESETS["4x4_camper"]         # 5,5 × 2,8, has_4wd


def _check(name, cond):
    assert cond, f"FALLO: {name}"
    print(f"  ok: {name}")


def test_small_camper_enters_everywhere():
    """Spot sin ninguna restricción conocida → camper pequeña APTA (no penaliza desconocido)."""
    v = evaluate(SpotConstraints(), SMALL)
    _check("camper pequeña + spot desconocido → strict APTO", v.strict == State.APTO)
    _check("camper pequeña + spot desconocido → conservador APTO", v.conservative == State.APTO)
    _check("no es exclusión dura", not v.is_hard_excluded)


def test_big_ac_unknown_is_caution_not_apto():
    """AC grande + spot totalmente desconocido → DESCONOCIDO estricto / PRECAUCIÓN conservador."""
    v = evaluate(SpotConstraints(), BIG)
    _check("AC grande + desconocido → strict DESCONOCIDO", v.strict == State.DESCONOCIDO)
    _check("AC grande + desconocido → conservador PRECAUCION", v.conservative == State.PRECAUCION)
    _check("AC grande NUNCA apta a ciegas", v.conservative != State.APTO)


def test_known_height_limit_hard_excludes():
    """Barrera de altura conocida por debajo del vehículo → NO_APTO duro."""
    v = evaluate(SpotConstraints(max_height_m=2.0), BIG)   # 2,0 m < 3,4 m
    _check("altura 2,0 m excluye AC de 3,4 m", v.strict == State.NO_APTO)
    _check("es exclusión dura", v.is_hard_excluded)


def test_height_limit_admits_small_and_gran_volumen():
    """Un límite de 2,80 m admite la camper pequeña (corrección de dominio del usuario)."""
    gv = VEHICLE_PRESETS["gran_volumen"]                   # 2,7 m
    v_small = evaluate(SpotConstraints(max_height_m=2.80), SMALL)
    v_gv = evaluate(SpotConstraints(max_height_m=2.80), gv)
    _check("límite 2,80 m NO excluye camper pequeña", v_small.strict != State.NO_APTO)
    _check("límite 2,80 m NO excluye gran volumen (2,7 m)", v_gv.strict != State.NO_APTO)


def test_hard_exclusion_wins_over_unknown():
    """Si una dimensión conocida excluye, el veredicto es NO_APTO aunque otras sean desconocidas."""
    v = evaluate(SpotConstraints(max_length_m=6.0), BIG)   # 8,5 m > 6,0 m; resto desconocido
    _check("longitud excluye aunque altura/anchura desconocidas", v.strict == State.NO_APTO)


def test_4wd_required():
    """Acceso solo-4x4: excluye a quien no tiene tracción, admite a quien sí.
    Dimensiones físicas conocidas y holgadas para aislar la variable tracción."""
    spot = SpotConstraints(requires_4wd=True, max_length_m=10.0, max_height_m=4.0, max_width_m=3.0)
    v_no = evaluate(spot, SMALL)        # pequeña pero sin 4x4
    v_yes = evaluate(spot, FOURX4)      # cabe en las dimensiones y tiene 4x4
    _check("solo-4x4 excluye vehículo sin tracción", v_no.strict == State.NO_APTO)
    _check("solo-4x4 admite vehículo con tracción", v_yes.strict == State.APTO)


def test_rough_surface_never_hard_excludes():
    """Superficie rugosa sin 4x4 NO es exclusión dura: precaución para grande, apto para pequeña."""
    spot = SpotConstraints(surface="dirt")
    v_small = evaluate(spot, SMALL)
    v_big = evaluate(spot, BIG)
    _check("superficie rugosa no excluye duro a nadie", not v_small.is_hard_excluded and not v_big.is_hard_excluded)
    _check("rugosa + pequeña → apto", v_small.conservative == State.APTO)
    _check("rugosa + grande → precaución", v_big.conservative == State.PRECAUCION)


def test_known_fit_is_apto():
    """Límites conocidos que el vehículo cumple holgadamente → APTO."""
    spot = SpotConstraints(max_length_m=10.0, max_height_m=4.0, max_width_m=3.0, requires_4wd=False)
    v = evaluate(spot, BIG)
    _check("AC grande cabe en spot amplio conocido → APTO", v.strict == State.APTO)


def main():
    tests = [
        test_small_camper_enters_everywhere,
        test_big_ac_unknown_is_caution_not_apto,
        test_known_height_limit_hard_excludes,
        test_height_limit_admits_small_and_gran_volumen,
        test_hard_exclusion_wins_over_unknown,
        test_4wd_required,
        test_rough_surface_never_hard_excludes,
        test_known_fit_is_apto,
    ]
    for t in tests:
        print(f"\n{t.__name__}:")
        t()
    print(f"\n✅ {len(tests)} tests OK")


if __name__ == "__main__":
    main()
