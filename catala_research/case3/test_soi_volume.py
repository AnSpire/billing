"""Pytest over the transpiled Python of the СОИ water-volume model.

This is the acceptance test suite: it exercises the *generated Python module*
(`_build/pyrun/soi/Soi_volume.py`) — the exact artifact that ships to the runtime —
not the Catala interpreter. Each scope is called directly; runtime-owned inputs
(meter readings, house register, resident count, billing calendar, regional norms)
are mocked with concrete values.

Build/refresh the package with `make python-pkg` (or `make pytest`, which depends on
it). The package layout mirrors what Catala emits: relative-imported stdlib `_en`
modules + `_internal` externals, with `catala_runtime` on sys.path.

Volumes are exact rationals (Catala `decimal` = arbitrary-precision fraction), so all
comparisons are exact via `Fraction`.
"""
import os
import sys
from fractions import Fraction

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "_build", "pyrun"))

from catala_runtime import Decimal, Integer, Option, Array, Unit  # noqa: E402
from soi import Soi_volume as m  # noqa: E402

# --- construction / comparison helpers -------------------------------------

NO_TERMS = Option(None)  # absent context => scope uses const_norms (0.0903) default

# premise-kind enum constructors (nullary => Unit payload)
NR = m.PremiseKind(m.PremiseKind.Code.Nonresidential, Unit())
RN = m.PremiseKind(m.PremiseKind.Code.ResidentialNoMeter, Unit())
RM = m.PremiseKind(m.PremiseKind.Code.ResidentialMetered, Unit())


def D(x):
    return Decimal(str(x))


def I(x):  # noqa: E743
    return Integer(x)


def eq(actual, expected):
    """Exact rational equality: Decimal subclasses Fraction."""
    return Fraction(actual) == Fraction(str(expected))


def premise(volume, area, kind):
    return m.Premise(volume=D(volume), area=D(area), kind=kind)


def registry(*rows):
    """rows: (volume, area, kind) tuples -> Array of Premise (list input)."""
    return Array([premise(v, a, k) for (v, a, k) in rows])


# --- thin scope wrappers ---------------------------------------------------

def no_meter(n_oi, area_i, area_common, premises, dip, dim):
    return m.no_meter_s_o_i(
        m.NoMeterSOIIn(
            n_oi_in=D(n_oi),
            area_i_in=D(area_i),
            area_common_in=D(area_common),
            premises_in=premises,
            days_in_period_in=I(dip),
            days_in_month_in=I(dim),
        )
    )


def cap(subject_norm, area_common, persons, dip, dim):
    return m.normative_cap(
        m.NormativeCapIn(
            terms_in=NO_TERMS,
            subject_norm_per_m2_in=D(subject_norm),
            area_common_in=D(area_common),
            persons_in=I(persons),
            days_in_period_in=I(dip),
            days_in_month_in=I(dim),
        )
    )


def cold(subject_norm, v_house, premises, v_hot, v_heat,
         area_i, area_common, persons, dip, dim):
    return m.metered_cold_s_o_i(
        m.MeteredColdSOIIn(
            terms_in=NO_TERMS,
            subject_norm_per_m2_in=D(subject_norm),
            v_house_in=D(v_house),
            premises_in=premises,
            v_hot_selfproduced_in=D(v_hot),
            v_heating_in=D(v_heat),
            area_i_in=D(area_i),
            area_common_in=D(area_common),
            persons_in=I(persons),
            days_in_period_in=I(dip),
            days_in_month_in=I(dim),
        )
    )


def hot(subject_norm, v_house, premises, v_energy,
        area_i, area_common, persons, dip, dim):
    return m.metered_hot_s_o_i(
        m.MeteredHotSOIIn(
            terms_in=NO_TERMS,
            subject_norm_per_m2_in=D(subject_norm),
            v_house_in=D(v_house),
            premises_in=premises,
            v_energy_in=D(v_energy),
            area_i_in=D(area_i),
            area_common_in=D(area_common),
            persons_in=I(persons),
            days_in_period_in=I(dip),
            days_in_month_in=I(dim),
        )
    )


# a registry of three premises (one of each kind, volume 1 each) whose areas sum
# to S_об = 1000; used across metered tests so all three classification arms run.
REG3 = registry((1, 500, NR), (1, 300, RN), (1, 200, RM))


# --- Без ОДПУ: V_i = N_ои × S_ои × (S_i / S_об) ----------------------------

def test_no_meter_basic():
    # 0.03*100=3.0 ; S_об=600+400=1000 ; share 50/1000=0.05 ; V_i=0.15
    reg = registry((0, 600, NR), (0, 400, RM))
    assert eq(no_meter(0.03, 50, 100, reg, 30, 30).volume_i, "0.15")


def test_no_meter_proration():
    # 3.0 * 15/30 = 1.5 ; V_i = 0.075
    reg = registry((0, 1000, NR))
    assert eq(no_meter(0.03, 50, 100, reg, 15, 30).volume_i, "0.075")


def test_no_meter_zero_total_area():
    # S_об = 0 -> divide-by-zero guard -> share 0 -> V_i 0
    reg = registry((0, 0, NR))
    assert eq(no_meter(0.03, 50, 100, reg, 30, 30).volume_i, "0")


def test_no_meter_zero_days_in_month():
    # prorate guard: no proration -> monthly value -> V_i = 0.15
    reg = registry((0, 1000, NR))
    assert eq(no_meter(0.03, 50, 100, reg, 10, 0).volume_i, "0.15")


# --- Нормативный потолок: min(subject_per_m2 × S_ои, 0.0903 × persons) ------

def test_cap_person_binds():
    # subject 0.03*100=3.0 ; person 0.0903*10=0.903 ; cap=0.903
    assert eq(cap(0.03, 100, 10, 30, 30).cap, "0.903")


def test_cap_subject_binds():
    # subject 0.03*10=0.3 ; person 0.0903*1000=90.3 ; cap=0.3
    assert eq(cap(0.03, 10, 1000, 30, 30).cap, "0.3")


def test_cap_proration():
    # both limits halved: subject 1.5, person 0.4515 ; cap=0.4515
    assert eq(cap(0.03, 100, 10, 15, 30).cap, "0.4515")


# --- ОДПУ, холодная вода ----------------------------------------------------

def test_cold_basic_cap_not_binding():
    # ΣV = 1+1+1 = 3 ; raw = 10-3-0.5-0.5 = 6.0 ; cap = min(30, 90.3) = 30
    # house_soi 6.0 ; share 50/1000 = 0.05 ; V_i = 0.3
    r = cold(0.03, 10, REG3, 0.5, 0.5, 50, 1000, 1000, 30, 30)
    assert eq(r.house_soi, "6.0")
    assert eq(r.volume_i, "0.3")


def test_cold_cap_binds():
    # same raw 6.0 but persons=10 -> person 0.903 < subject 30 -> cap 0.903
    # house_soi = min(6.0, 0.903) = 0.903 ; V_i = 0.903*0.05 = 0.04515
    r = cold(0.03, 10, REG3, 0.5, 0.5, 50, 1000, 10, 30, 30)
    assert eq(r.house_soi, "0.903")
    assert eq(r.volume_i, "0.04515")


def test_cold_negative_clamp():
    # raw = 1-3-1-1 = -4 -> clamp 0 ; house_soi 0 ; V_i 0
    r = cold(0.03, 1, REG3, 1, 1, 50, 1000, 1000, 30, 30)
    assert eq(r.house_soi, "0")
    assert eq(r.volume_i, "0")


# --- ОДПУ, горячая вода -----------------------------------------------------

def test_hot_basic():
    # ΣV = 3 ; raw = 8-3-1 = 4.0 ; cap 30 (not binding) ; house_soi 4.0 ; V_i 0.2
    r = hot(0.03, 8, REG3, 1, 50, 1000, 1000, 30, 30)
    assert eq(r.house_soi, "4.0")
    assert eq(r.volume_i, "0.2")


def test_hot_negative_clamp():
    # raw = 1-3-1 = -3 -> clamp 0 ; house_soi 0 ; V_i 0
    r = hot(0.03, 1, REG3, 1, 50, 1000, 1000, 30, 30)
    assert eq(r.house_soi, "0")
    assert eq(r.volume_i, "0")


# --- classification: all three category sums contribute --------------------

def test_classification_sums():
    # distinct volumes per kind; all subtracted with coeff 1 => ΣV = 2+5+8 = 15
    # raw = 100 - 15 = 85 ; cap not binding
    reg = registry((2, 100, NR), (5, 100, RN), (8, 100, RM))
    r = cold(1.0, 100, reg, 0, 0, 50, 1000, 1000000, 30, 30)
    assert eq(r.house_soi, "85.0")


# --- override the terms context (reusability / provenance pinning) ---------

def test_terms_override_changes_federal_norm():
    """A different federal per-person norm flows through via the `terms` context.

    Doubling per_person_norm to 0.1806 doubles the person limit; with a small
    resident count it becomes binding: cap = min(0.03*100=3.0, 0.1806*10=1.806).
    """
    from catala_runtime import CatalaTuple, SourcePosition

    other = m.Norms(per_person_norm=D("0.1806"))
    pos = SourcePosition(filename="test", start_line=0, end_line=0,
                         start_column=0, end_column=0, law_headings=[])
    r = m.normative_cap(
        m.NormativeCapIn(
            terms_in=Option(CatalaTuple(other, pos)),
            subject_norm_per_m2_in=D("0.03"),
            area_common_in=D(100), persons_in=I(10),
            days_in_period_in=I(30), days_in_month_in=I(30),
        )
    )
    assert eq(r.cap, "1.806")
