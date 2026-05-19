"""Ability ID -> English name (generated from PKHeX Ability.cs).

Index = ability ID (0 = none). Covers Gen 6/7 IDs and beyond.
"""

ABILITY_NAMES = [
    '', 'Stench', 'Drizzle', 'Speed Boost', 'Battle Armor', 'Sturdy',
    'Damp', 'Limber', 'Sand Veil', 'Static', 'Volt Absorb', 'Water Absorb',
    'Oblivious', 'Cloud Nine', 'Compound Eyes', 'Insomnia', 'Color Change', 'Immunity',
    'Flash Fire', 'Shield Dust', 'Own Tempo', 'Suction Cups', 'Intimidate', 'Shadow Tag',
    'Rough Skin', 'Wonder Guard', 'Levitate', 'Effect Spore', 'Synchronize', 'Clear Body',
    'Natural Cure', 'Lightning Rod', 'Serene Grace', 'Swift Swim', 'Chlorophyll', 'Illuminate',
    'Trace', 'Huge Power', 'Poison Point', 'Inner Focus', 'Magma Armor', 'Water Veil',
    'Magnet Pull', 'Soundproof', 'Rain Dish', 'Sand Stream', 'Pressure', 'Thick Fat',
    'Early Bird', 'Flame Body', 'Run Away', 'Keen Eye', 'Hyper Cutter', 'Pickup',
    'Truant', 'Hustle', 'Cute Charm', 'Plus', 'Minus', 'Forecast',
    'Sticky Hold', 'Shed Skin', 'Guts', 'Marvel Scale', 'Liquid Ooze', 'Overgrow',
    'Blaze', 'Torrent', 'Swarm', 'Rock Head', 'Drought', 'Arena Trap',
    'Vital Spirit', 'White Smoke', 'Pure Power', 'Shell Armor', 'Air Lock', 'Tangled Feet',
    'Motor Drive', 'Rivalry', 'Steadfast', 'Snow Cloak', 'Gluttony', 'Anger Point',
    'Unburden', 'Heatproof', 'Simple', 'Dry Skin', 'Download', 'Iron Fist',
    'Poison Heal', 'Adaptability', 'Skill Link', 'Hydration', 'Solar Power', 'Quick Feet',
    'Normalize', 'Sniper', 'Magic Guard', 'No Guard', 'Stall', 'Technician',
    'Leaf Guard', 'Klutz', 'Mold Breaker', 'Super Luck', 'Aftermath', 'Anticipation',
    'Forewarn', 'Unaware', 'Tinted Lens', 'Filter', 'Slow Start', 'Scrappy',
    'Storm Drain', 'Ice Body', 'Solid Rock', 'Snow Warning', 'Honey Gather', 'Frisk',
    'Reckless', 'Multitype', 'Flower Gift', 'Bad Dreams', 'Pickpocket', 'Sheer Force',
    'Contrary', 'Unnerve', 'Defiant', 'Defeatist', 'Cursed Body', 'Healer',
    'Friend Guard', 'Weak Armor', 'Heavy Metal', 'Light Metal', 'Multiscale', 'Toxic Boost',
    'Flare Boost', 'Harvest', 'Telepathy', 'Moody', 'Overcoat', 'Poison Touch',
    'Regenerator', 'Big Pecks', 'Sand Rush', 'Wonder Skin', 'Analytic', 'Illusion',
    'Imposter', 'Infiltrator', 'Mummy', 'Moxie', 'Justified', 'Rattled',
    'Magic Bounce', 'Sap Sipper', 'Prankster', 'Sand Force', 'Iron Barbs', 'Zen Mode',
    'Victory Star', 'Turboblaze', 'Teravolt', 'Aroma Veil', 'Flower Veil', 'Cheek Pouch',
    'Protean', 'Fur Coat', 'Magician', 'Bulletproof', 'Competitive', 'Strong Jaw',
    'Refrigerate', 'Sweet Veil', 'Stance Change', 'Gale Wings', 'Mega Launcher', 'Grass Pelt',
    'Symbiosis', 'Tough Claws', 'Pixilate', 'Gooey', 'Aerilate', 'Parental Bond',
    'Dark Aura', 'Fairy Aura', 'Aura Break', 'Primordial Sea', 'Desolate Land', 'Delta Stream',
    'Stamina', 'Wimp Out', 'Emergency Exit', 'Water Compaction', 'Merciless', 'Shields Down',
    'Stakeout', 'Water Bubble', 'Steelworker', 'Berserk', 'Slush Rush', 'Long Reach',
    'Liquid Voice', 'Triage', 'Galvanize', 'Surge Surfer', 'Schooling', 'Disguise',
    'Battle Bond', 'Power Construct', 'Corrosion', 'Comatose', 'Queenly Majesty', 'Innards Out',
    'Dancer', 'Battery', 'Fluffy', 'Dazzling', 'Soul Heart', 'Tangling Hair',
    'Receiver', 'Powerof Alchemy', 'Beast Boost', 'RKS System', 'Electric Surge', 'Psychic Surge',
    'Misty Surge', 'Grassy Surge', 'Full Metal Body', 'Shadow Shield', 'Prism Armor', 'Neuroforce',
    'Intrepid Sword', 'Dauntless Shield', 'Libero', 'Ball Fetch', 'Cotton Down', 'Propeller Tail',
    'Mirror Armor', 'Gulp Missile', 'Stalwart', 'Steam Engine', 'Punk Rock', 'Sand Spit',
    'Ice Scales', 'Ripen', 'Ice Face', 'Power Spot', 'Mimicry', 'Screen Cleaner',
    'Steely Spirit', 'Perish Body', 'Wandering Spirit', 'Gorilla Tactics', 'Neutralizing Gas', 'Pastel Veil',
    'Hunger Switch', 'Quick Draw', 'Unseen Fist', 'Curious Medicine', 'Transistor', 'Dragons Maw',
    'Chilling Neigh', 'Grim Neigh', 'As One I', 'As One G', 'Lingering Aroma', 'Seed Sower',
    'Thermal Exchange', 'Anger Shell', 'Purifying Salt', 'Well Baked Body', 'Wind Rider', 'Guard Dog',
    'Rocky Payload', 'Wind Power', 'Zeroto Hero', 'Commander', 'Electromorphosis', 'Protosynthesis',
    'Quark Drive', 'Goodas Gold', 'Vesselof Ruin', 'Swordof Ruin', 'Tabletsof Ruin', 'Beadsof Ruin',
    'Orichalcum Pulse', 'Hadron Engine', 'Opportunist', 'Cud Chew', 'Sharpness', 'Supreme Overlord',
    'Costar', 'Toxic Debris', 'Armor Tail', 'Earth Eater', 'Mycelium Might', 'Hospitality',
    'Minds Eye', 'Embody Aspect0', 'Embody Aspect1', 'Embody Aspect2', 'Embody Aspect3', 'Toxic Chain',
    'Supersweet Syrup', 'MAX_COUNT',
]


def ability_name(ability_id) -> str:
    """Name for an ability ID; "#<id>" if out of range/unknown."""
    try:
        i = int(ability_id)
    except (TypeError, ValueError):
        return "?"
    if 0 < i < len(ABILITY_NAMES) and ABILITY_NAMES[i]:
        return ABILITY_NAMES[i]
    return f"#{i}"
