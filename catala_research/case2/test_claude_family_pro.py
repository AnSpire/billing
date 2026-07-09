"""Pytest over the transpiled Python of the «Claude Family Pro» model.

This is the acceptance test suite: it exercises the *generated Python module*
(`_build/pyrun/cfp/Claude_family_pro.py`) — the exact artifact that ships to the
runtime — not the Catala interpreter. Each scope is called directly; runtime-owned
inputs (carried balance, tenure streak, referral flag, SLA availability, VAT rate,
termination calendar) are mocked with concrete values.

Build/refresh the package with `make python-pkg` (or `make pytest`, which depends
on it). The package layout mirrors what Catala emits: relative-imported stdlib
`_en` modules + `_internal` externals, with `catala_runtime` on sys.path.
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "_build", "pyrun"))

from catala_runtime import Decimal, Money, Integer, Option, Array, Bool  # noqa: E402
from cfp import Claude_family_pro as m  # noqa: E402

# --- construction / comparison helpers -------------------------------------

F, T = Bool(False), Bool(True)
NO_TARIFF = Option(None)  # absent context => scope uses const_tariff default


def D(x):
    return Decimal(x)


def I(x):  # noqa: E743
    return Integer(x)


def MO(x):
    return Money(str(x))


def cents(x):
    """Money is stored as an integer number of kopecks; int() exposes it."""
    return int(x)


def member(mid, normalized, child=False, owner=False):
    return m.Member(
        member_id=I(mid),
        normalized=D(normalized),
        is_child=T if child else F,
        is_owner=T if owner else F,
    )


def usage(u, cr, cw, out, think):
    return m.TokenUsage(
        input_uncached=D(u),
        input_cache_read=D(cr),
        input_cache_write=D(cw),
        output_regular=D(out),
        output_thinking=D(think),
    )


# --- thin scope wrappers (context defaults to const_tariff) ----------------

def normalize(u):
    return m.token_normalization(
        m.TokenNormalizationIn(tariff_in=NO_TARIFF, usage_in=u)
    )


def allowance(is_term, active_days, days_in_month):
    return m.period_allowance(
        m.PeriodAllowanceIn(
            tariff_in=NO_TARIFF,
            is_termination_in=T if is_term else F,
            active_days_in=I(active_days),
            days_in_month_in=I(days_in_month),
        )
    )


def overage_comp(total, carried, included, carry_ok):
    return m.overage_computation(
        m.OverageComputationIn(
            tariff_in=NO_TARIFF,
            total_consumption_in=D(total),
            carried_balance_in=D(carried),
            included_volume_in=D(included),
            carryover_allowed_in=T if carry_ok else F,
        )
    )


def overage_charge(over, fair_use):
    return m.overage_charge(
        m.OverageChargeIn(
            tariff_in=NO_TARIFF,
            overage_in=D(over),
            fair_use_excess_in=D(fair_use),
        )
    )


def loyalty_referral(after_tier, fee, periods, referral):
    return m.loyalty_referral_discount(
        m.LoyaltyReferralDiscountIn(
            tariff_in=NO_TARIFF,
            charge_after_tier_in=MO(after_tier),
            subscription_fee_in=MO(fee),
            consecutive_paid_periods_in=I(periods),
            referral_active_in=T if referral else F,
        )
    )


def discount_total(fee, gross_over, tier_disc, combined, is_term):
    return m.discount_cap_and_total(
        m.DiscountCapAndTotalIn(
            tariff_in=NO_TARIFF,
            subscription_fee_in=MO(fee),
            gross_overage_in=MO(gross_over),
            tier_discount_in=MO(tier_disc),
            combined_loyalty_referral_in=MO(combined),
            is_termination_in=T if is_term else F,
        )
    )


def sla(avail, fee):
    return m.s_l_a_compensation(
        m.SLACompensationIn(
            tariff_in=NO_TARIFF,
            availability_in=D(avail),
            subscription_fee_in=MO(fee),
        )
    )


def final_bill(pretax, comp, vat):
    return m.final_bill(
        m.FinalBillIn(
            pretax_in=MO(pretax),
            compensation_in=MO(comp),
            vat_rate_in=D(vat),
        )
    )


def distribute(group_charge, members):
    return m.participant_distribution(
        m.ParticipantDistributionIn(
            group_charge_in=MO(group_charge),
            members_in=Array(members),
        )
    )


def group_bill(members, carried=0, is_term=False, active_days=30, days=30,
               periods=5, referral=False, avail="0.999", vat="0.20"):
    return m.group_period_bill(
        m.GroupPeriodBillIn(
            tariff_in=NO_TARIFF,
            members_in=Array(members),
            carried_balance_in=D(carried),
            is_termination_in=T if is_term else F,
            active_days_in=I(active_days),
            days_in_month_in=I(days),
            consecutive_paid_periods_in=I(periods),
            referral_active_in=T if referral else F,
            availability_in=D(avail),
            vat_rate_in=D(vat),
        )
    )


def charge_of(allocations, mid):
    return sum(cents(c.charge) for c in allocations if int(c.member_id) == mid)


# ===========================================================================
# §1.2 — token normalization
# ===========================================================================

def test_normalization():
    r = normalize(usage(1000, 1000, 1000, 1000, 1000))
    assert r.normalized == D(12350)


# ===========================================================================
# §2 — subscription fee & included volume (proration on termination)
# ===========================================================================

def test_allowance_normal():
    r = allowance(False, 30, 30)
    assert cents(r.subscription_fee) == cents(MO("2490.00"))
    assert r.included_volume == D(12000000)
    assert bool(r.carryover_allowed) is True


def test_allowance_termination_half():
    r = allowance(True, 15, 30)
    assert cents(r.subscription_fee) == cents(MO("1245.00"))
    assert r.included_volume == D(6000000)
    assert bool(r.carryover_allowed) is False


def test_allowance_termination_rounding():
    # 12M*10/31 = 3_870_967.7 -> floor to 100k step = 3_800_000
    # 2490*10/31 = 803.2258 -> floor to kopeck = 803.22 (NOT 803.23 nearest)
    r = allowance(True, 10, 31)
    assert r.included_volume == D(3800000)
    assert cents(r.subscription_fee) == cents(MO("803.22"))


# ===========================================================================
# §3.3 + §4 — overage & carryover
# ===========================================================================

def test_overage_under_included():
    r = overage_comp(10_000_000, 0, 12_000_000, True)
    assert r.overage == D(0)
    assert r.carryover_to_next == D(2_000_000)


def test_carryover_capped_at_20pct():
    r = overage_comp(8_000_000, 0, 12_000_000, True)
    assert r.carryover_to_next == D(2_400_000)


def test_overage_sequential_carry_then_included():
    # carry 1M first, then 12M included, overage = 15 - 1 - 12 = 2M
    r = overage_comp(15_000_000, 1_000_000, 12_000_000, True)
    assert r.overage == D(2_000_000)
    assert r.carryover_to_next == D(0)


def test_no_carryover_on_termination():
    r = overage_comp(5_000_000, 0, 6_000_000, False)
    assert r.carryover_to_next == D(0)


# ===========================================================================
# §3.1 + §5.1–5.3 — overage tariff & discounts
# ===========================================================================

def test_charge_tier1_no_discount():
    r = overage_charge(1_000_000, 0)
    assert cents(r.gross_charge) == cents(MO("420.00"))
    assert cents(r.tier_discount) == 0
    assert cents(r.charge_after_tier) == cents(MO("420.00"))


def test_charge_tier2():
    r = overage_charge(7_000_000, 0)
    assert cents(r.gross_charge) == cents(MO("2940.00"))
    assert cents(r.tier_discount) == cents(MO("210.00"))
    assert cents(r.charge_after_tier) == cents(MO("2730.00"))


def test_charge_tier3():
    r = overage_charge(12_000_000, 0)
    assert cents(r.gross_charge) == cents(MO("5040.00"))
    assert cents(r.tier_discount) == cents(MO("487.20"))
    assert cents(r.charge_after_tier) == cents(MO("4552.80"))


def test_charge_loyalty_flat_25():
    r = overage_charge(25_000_000, 0)
    assert cents(r.gross_charge) == cents(MO("10500.00"))
    assert cents(r.tier_discount) == cents(MO("2625.00"))
    assert cents(r.charge_after_tier) == cents(MO("7875.00"))


def test_charge_fair_use_excluded():
    # 25M overage, 5M is single-member consumption over 30M -> full rate.
    # discountable 20M, loyalty 25% -> discount $2100, net $8400.
    r = overage_charge(25_000_000, 5_000_000)
    assert cents(r.gross_charge) == cents(MO("10500.00"))
    assert cents(r.tier_discount) == cents(MO("2100.00"))
    assert cents(r.charge_after_tier) == cents(MO("8400.00"))


# ===========================================================================
# §5.4–5.5 — tenure & referral (not summed: greater applies)
# ===========================================================================

def test_tenure_only():
    r = loyalty_referral("2730.00", "2490.00", 13, False)
    assert cents(r.combined_discount) == cents(MO("136.50"))


def test_referral_only():
    r = loyalty_referral("2730.00", "2490.00", 5, True)
    assert cents(r.combined_discount) == cents(MO("498.00"))


def test_tenure_and_referral_takes_greater():
    r = loyalty_referral("2730.00", "2490.00", 13, True)
    assert cents(r.combined_discount) == cents(MO("498.00"))


def test_no_loyalty_discount():
    r = loyalty_referral("2730.00", "2490.00", 5, False)
    assert cents(r.combined_discount) == 0


# ===========================================================================
# §5.6 + §2.3 — discount cap & minimum
# ===========================================================================

def test_pretax_normal():
    r = discount_total("2490.00", "2940.00", "210.00", "0.00", False)
    assert cents(r.total_discount) == cents(MO("210.00"))
    assert cents(r.pretax) == cents(MO("5220.00"))


def test_cap_binds_and_floor_applies():
    # raw discount 5000 -> capped to 30%*(2490+1000)=1047; pretax_raw 2443 < 2490
    # -> floored back up to the subscription fee.
    r = discount_total("2490.00", "1000.00", "5000.00", "0.00", False)
    assert cents(r.total_discount) == cents(MO("1047.00"))
    assert cents(r.pretax) == cents(MO("2490.00"))


def test_floor_no_overage():
    # referral discount 498 on a no-overage period floored back up to the fee.
    r = discount_total("2490.00", "0.00", "0.00", "498.00", False)
    assert cents(r.pretax) == cents(MO("2490.00"))


def test_no_floor_on_termination():
    r = discount_total("1245.00", "0.00", "0.00", "0.00", True)
    assert cents(r.pretax) == cents(MO("1245.00"))


# ===========================================================================
# §7 — SLA compensation (threshold boundaries)
# ===========================================================================

@pytest.mark.parametrize("avail,expected", [
    ("0.94", "2490.00"),    # < 95% -> 100%
    ("0.95", "747.00"),     # boundary: in [95%, 98%) -> 30%
    ("0.97", "747.00"),     # 30%
    ("0.98", "249.00"),     # boundary: in [98%, 99.5%) -> 10%
    ("0.994", "249.00"),    # 10%
    ("0.995", "0.00"),      # boundary: >= 99.5% -> none (strict "< 99.5%")
    ("0.999", "0.00"),      # none
])
def test_sla_compensation(avail, expected):
    r = sla(avail, "2490.00")
    assert cents(r.compensation) == cents(MO(expected))


# ===========================================================================
# §8 — VAT & total
# ===========================================================================

def test_final_normal():
    r = final_bill("5220.00", "0.00", "0.20")
    assert cents(r.vat_amount) == cents(MO("1044.00"))
    assert cents(r.total_due) == cents(MO("6264.00"))


def test_final_with_compensation():
    r = final_bill("2490.00", "249.00", "0.20")
    assert cents(r.total_due) == cents(MO("2689.20"))


def test_final_compensation_exceeds_pretax():
    r = final_bill("2490.00", "2490.00", "0.20")
    assert cents(r.total_due) == 0


def test_final_retroactive_vat_change():
    # §8.2: a retroactive VAT rate is just a different runtime input.
    r18 = final_bill("1000.00", "0.00", "0.18")
    r20 = final_bill("1000.00", "0.00", "0.20")
    assert cents(r18.total_due) == cents(MO("1180.00"))
    assert cents(r20.total_due) == cents(MO("1200.00"))


# ===========================================================================
# §6 — distribution among members
# ===========================================================================

def test_distribution_clean():
    members = [
        member(1, 60_000_000, owner=True),
        member(2, 30_000_000),
        member(3, 10_000_000),
    ]
    r = distribute("99.00", members)
    assert charge_of(r.allocations, 2) == cents(MO("29.70"))
    assert charge_of(r.allocations, 3) == cents(MO("9.90"))
    assert cents(r.owner_charge) == cents(MO("59.40"))
    # invariant: allocations sum exactly to the group charge
    assert sum(cents(c.charge) for c in r.allocations) == cents(MO("99.00"))


def test_distribution_round_down_remainder_to_owner():
    members = [member(1, 1, owner=True), member(2, 1), member(3, 1)]
    r = distribute("100.00", members)
    assert charge_of(r.allocations, 2) == cents(MO("33.33"))
    assert charge_of(r.allocations, 3) == cents(MO("33.33"))
    assert cents(r.owner_charge) == cents(MO("33.34"))  # owner absorbs $0.01
    assert sum(cents(c.charge) for c in r.allocations) == cents(MO("100.00"))


def test_distribution_child_charged_to_owner():
    members = [
        member(1, 10_000_000, owner=True),
        member(2, 20_000_000, child=True),
        member(3, 10_000_000),
    ]
    r = distribute("90.00", members)
    assert charge_of(r.allocations, 2) == 0          # child pays nothing
    assert charge_of(r.allocations, 3) == cents(MO("22.50"))
    assert cents(r.owner_charge) == cents(MO("67.50"))  # owner absorbs child
    assert sum(cents(c.charge) for c in r.allocations) == cents(MO("90.00"))


def test_distribution_zero_consumption():
    members = [member(1, 0, owner=True), member(2, 0)]
    r = distribute("50.00", members)
    assert cents(r.owner_charge) == cents(MO("50.00"))


# ===========================================================================
# End-to-end GroupPeriodBill
# ===========================================================================

def test_e2e_normal():
    members = [member(1, 8_000_000, owner=True), member(2, 6_000_000)]
    r = group_bill(members)
    assert r.overage == D(2_000_000)
    assert cents(r.pretax) == cents(MO("3330.00"))
    assert cents(r.total_due) == cents(MO("3996.00"))
    assert cents(r.owner_charge) == cents(MO("1902.86"))
    alloc = {int(c.member_id): cents(c.charge) for c in r.allocations}
    assert alloc[2] == cents(MO("1427.14"))


def test_e2e_loyalty_fairuse_child_referral_sla():
    members = [
        member(1, 35_000_000, owner=True),    # 5M over the 30M fair-use cap
        member(2, 5_000_000, child=True),     # child -> owner absorbs
    ]
    r = group_bill(members, periods=13, referral=True, avail="0.97")
    assert r.overage == D(28_000_000)
    assert cents(r.gross_overage) == cents(MO("11760.00"))
    assert cents(r.total_discount) == cents(MO("2913.00"))   # 2415 tier + 498 referral
    assert cents(r.pretax) == cents(MO("11337.00"))
    assert cents(r.compensation) == cents(MO("747.00"))      # SLA 97% -> 30%
    assert cents(r.total_due) == cents(MO("12708.00"))
    assert cents(r.owner_charge) == cents(MO("11337.00"))    # child fully on owner


def test_e2e_termination_prorated():
    members = [member(1, 3_000_000, owner=True), member(2, 2_000_000)]
    r = group_bill(members, is_term=True, active_days=15, days=30)
    assert r.overage == D(0)                       # 5M < prorated 6M included
    assert r.carryover_to_next == D(0)             # §4.3 no carryover
    assert cents(r.pretax) == cents(MO("1245.00"))  # §2.3 floor does NOT apply
    assert cents(r.total_due) == cents(MO("1494.00"))
    assert cents(r.owner_charge) == cents(MO("747.00"))


def test_e2e_carried_balance_mocked_state():
    # Runtime state: 3M carried in from last period is consumed before the
    # included volume, so a 14M group with 3M carry sees only 14-3-12 = -1 -> 0
    # overage; wait: 14 - 3 = 11 <= 12 included -> overage 0, and 1M of included
    # remains unused -> carryover 1M forms for next period.
    members = [member(1, 9_000_000, owner=True), member(2, 5_000_000)]
    r = group_bill(members, carried=3_000_000)
    assert r.overage == D(0)
    assert r.carryover_to_next == D(1_000_000)
    assert cents(r.pretax) == cents(MO("2490.00"))  # only the subscription fee


def test_tariff_override_reuses_scope():
    # §9.1 provenance / reusability: a different contract instance overrides the
    # pinned terms via the context input instead of touching the rule.
    from catala_runtime import CatalaTuple, SourcePosition
    base = m.const_tariff
    fields = {s: getattr(base, s) for s in base.__slots__}
    fields["subscription_fee"] = MO("999.00")  # structs are immutable: override here
    custom = m.Tariff(**fields)
    tin = Option(CatalaTuple(custom, SourcePosition(filename="test", start_line=0,
                 start_column=0, end_line=0, end_column=0, law_headings=[])))
    r = m.period_allowance(m.PeriodAllowanceIn(
        tariff_in=tin, is_termination_in=F,
        active_days_in=I(30), days_in_month_in=I(30)))
    assert cents(r.subscription_fee) == cents(MO("999.00"))
