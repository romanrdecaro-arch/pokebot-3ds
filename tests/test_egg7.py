"""
Gen 7 egg prediction (pokebot/egg7.py), a port of 3DSRNGTool's Egg7.

The expected eggs below came from 3DSRNGTool's own Egg7.cs / EggRNG.cs /
TinyMT.cs, compiled unchanged into a harness (2026-11-01); 45,000 random
eggs across every item / gender / Masuda / Shiny Charm combination
matched the port exactly. These few pin it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pokebot import egg7 as e  # noqa: E402

SEED = (305419896, 2271560481, 3735928559, 3237998016)


def tinymt_init(seed):
    """TinyMT32's seeding (used here only to check against its reference)."""
    st = [seed, 0x8F7011EE, 0xFC78FF1F, 0x3793FDFF]
    for i in range(1, 8):
        p = st[(i - 1) & 3]
        st[i & 3] ^= (i + 1812433253 * (p ^ (p >> 30))) & 0xFFFFFFFF
    return e.advance(tuple(st), 8)


def test_tinymt_matches_its_reference_output():
    # MersenneTwister-Lab/TinyMT tinymt/check32.out.txt, seed = 1
    rand = e.Stream(tinymt_init(1))
    assert [rand[i] for i in range(5)] == [
        2545341989, 981918433, 3715302833, 2387538352, 3591001365]


def eggs(seed, parents, tsv, n=3):
    rand = e.Stream(seed)
    return [(x.pid, x.ec, x.nature, x.shiny, x.used)
            for x in (e.generate(rand, f, parents, tsv) for f in range(n))]


def test_masuda_and_charm_with_a_destiny_knot():
    p = e.Parents(0, 2, random_gender=True, same_species=False,
                  masuda=True, shiny_charm=True)
    assert eggs(SEED, p, 1234) == [
        (3808399956, 2647530436, 10, False, 30),
        (1628819277, 973706921, 4, False, 38),
        (1628819277, 973706921, 11, False, 37)]


def test_masuda_with_two_power_items_and_one_species():
    p = e.Parents(3, 8, random_gender=False, same_species=True,
                  masuda=True, shiny_charm=False)
    assert eggs(SEED, p, 1234) == [
        (2647530436, 1402562016, 5, False, 23),
        (4094300883, 1047247762, 10, False, 26),
        (4094300883, 1047247762, 4, False, 25)]


def test_charm_with_two_everstones():
    p = e.Parents(1, 1, random_gender=True, same_species=False,
                  masuda=False, shiny_charm=True)
    seed = (2882400001, 16909060, 4278190335, 2863311530)
    assert eggs(seed, p, 777) == [
        (608901063, 3386906775, 9, False, 23),
        (2379118770, 2752957166, 21, False, 21),
        (141351144, 2379118770, 8, False, 22)]


def test_the_first_shiny_egg_is_the_one_3dsrngtool_finds():
    p = e.Parents(0, 2, True, False, True, True)
    n = e.eggs_until_shiny(SEED, p, 1234)
    assert n == 2530
    egg = e.generate(e.Stream(SEED), n, p, 1234)
    assert (egg.pid, egg.ec, egg.nature, egg.shiny, egg.used) == (
        1796286001, 3149212617, 13, True, 36)


def test_the_egg_after_n_declines_is_the_seed_moved_n_steps():
    p = e.Parents(0, 2, True, False, True, False)
    for n in (0, 1, 7, 40):
        assert (e.generate(e.Stream(SEED), n, p, 99)
                == e.generate(e.Stream(e.advance(SEED, n)), 0, p, 99))


def test_no_masuda_and_no_charm_draws_no_pid():
    p = e.Parents(0, 0, True, False, False, False)
    egg = e.generate(e.Stream(SEED), 0, p, 0)
    assert egg.pid is None and not egg.shiny and p.pid_rolls == 0


def mon(species, gender="M", item=0, lang=2):
    return NS(species=species, gender=gender, held_item=item, ot_language=lang)


def test_parents_read_from_the_nursery():
    # Garchomp F with a Destiny Knot, Japanese; Garchomp M with an Everstone
    p = e.Parents.from_nursery(mon(445, "M", 229, 2), mon(445, "F", 280, 1),
                               shiny_charm=False)
    assert (p.male_item, p.female_item) == (1, 2)
    assert p.same_species and p.masuda and p.random_gender
    assert p.pid_rolls == 6


def test_a_ditto_takes_the_role_its_partner_does_not():
    ditto = mon(e.DITTO, "G", 294)                      # Power Weight: HP
    female = e.Parents.from_nursery(ditto, mon(25, "F", 289), True)
    assert (female.male_item, female.female_item) == (3, 4)
    male = e.Parents.from_nursery(ditto, mon(25, "M", 289), True)
    assert (male.male_item, male.female_item) == (4, 3)
    assert not (female.same_species or female.masuda)
    assert female.pid_rolls == 2


def test_gender_draws_follow_the_egg_not_the_parent():
    tauros = e.Parents.from_nursery(mon(e.DITTO, "G"), mon(128), False)
    gallade = e.Parents.from_nursery(mon(e.DITTO, "G"), mon(475), False)
    nidoking = e.Parents.from_nursery(mon(e.DITTO, "G"), mon(34), False)
    assert not tauros.random_gender      # Tauros eggs are always male
    assert gallade.random_gender         # Gallade's eggs are Ralts
    assert nidoking.random_gender        # Nidoran: either species, a draw
