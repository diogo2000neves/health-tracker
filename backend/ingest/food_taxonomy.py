"""Canonical names and nutritionist-grade groups for the foods the user logs.

This is the layer the coach was missing. The meal log is written by a vision model
in whatever words fit the photo, so the same food arrives under many names:

    cod / boiled cod / boiled cod (bacalhau)
    beef / beef steak / beef patty / beef burger patty / angus beef burger patty
    ham / cooked ham / sliced ham
    arroz branco / white rice

Counted raw, that vocabulary is 116 "foods" over 28 days with 85 of them appearing
exactly once — so every food-level rule ("red meat most days", "no fish in two
weeks") silently reads as zero and the coach is left with nothing to say but
nutrient arithmetic. Canonicalising first is what turns the log into something a
nutritionist could actually read.

Three stages, cheapest first:

  1. `normalize` — pure string work: accents, parentheticals, brand names, cooking
     methods and qualifiers come off, so "boiled cod (bacalhau)" and "cod" collapse
     to one key. Deterministic and unit-tested.
  2. `ALIASES` / `GROUP_RULES` — a curated table over the normalised name: the
     pt-PT ↔ en synonyms, and the keyword rules that assign a food to a group the
     way a dietitian would split it (refined vs whole grains, red vs processed
     meat, oily vs white fish). Keywords are normalised with the same function they
     are matched against, and matched as whole token runs — so "pea" can never
     claim "pear" and "potato" can never claim "potato chips".
  3. `classify_unknown` — only for names the rules can't place: one Gemini call,
     cached in the taxonomy blob forever. New foods are rare and each is classified
     once, so the aggregation stays deterministic over reference data the model only
     ever *contributes* to — the same division of labour as the nutrition audit
     (model for perception, code for the arithmetic).

Groups carry a posture (`more` / `less` / `neutral`) and weekly reference servings
from mainstream dietary guidance (fish twice a week, legumes three times, processed
meat as little as possible, most grains whole). Those are framing for observations
about what was logged — never diagnoses.

The module also owns the other half of a food's identity: its **pt-PT display
name** (`display_pt`, `translate_unknown`). The keys above are English by necessity
— FDC is a USDA database and every rule here is written against English — but the
app and the coach are Portuguese, so a food that is counted as "chicken breast" has
to be *read* as "peito de frango". Same division of labour as the groups: a curated
table first, one cached model call only for what it can't place.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("food_taxonomy")

# -- stage 1: string normalisation ---------------------------------------------

# Brand and retailer words carry no nutritional meaning but split the vocabulary
# ("pingo doce macedónia de vegetais" vs "garden vegetable macedoine").
_BRANDS = (
    "pingo doce", "go active", "nestle", "lindahls", "oikos", "mimosa", "buitoni",
    "danone", "activia", "yoplait", "continente", "lidl", "auchan", "iogurte magro",
    "i love kefir",
)

# Cooking methods and qualifiers. Preparation matters nutritionally in exactly one
# case — frying — so `is_fried` reads the raw name BEFORE these come off, and the
# canonical name stays method-free for grouping.
#
# "whole" is deliberately NOT here: "whole grain" vs "grain" is the single most
# important distinction this module makes.
_QUALIFIERS = (
    "boiled", "cooked", "raw", "grilled", "roasted", "roast", "baked", "toasted",
    "pan seared", "seared", "breaded", "fried", "deep fried", "air fryer",
    "airfryer", "steamed", "smashed", "mashed", "sliced", "chopped", "shredded",
    "minced", "ground", "homemade", "freshly squeezed", "fresh", "frozen",
    "canned", "dried", "salted", "unsalted", "sweetened", "unsweetened", "natural",
    "plain", "light", "lean", "extra", "small", "large", "medium", "semi hard",
    "hard", "soft", "skimmed", "semi skimmed", "low fat", "high protein",
    "lactose free", "shoestring", "assorted", "assortment", "mix", "mixed", "side",
    "cozido", "cozida", "grelhado", "grelhada", "assado", "assada", "frito",
    "frita", "cru", "crua", "fatiado", "caseiro", "caseira",
)

# Words that only glue a name together.
_STOPWORDS = ("de", "da", "do", "dos", "das", "com", "e", "of", "with", "and",
              "the", "a", "o", "os", "as", "in", "style")

_FRIED_MARKERS = ("fried", "fries", "frito", "frita", "chips", "crisps", "tempura",
                  "breaded", "panado", "panada")

# Words that only *look* plural. Chopping the "s" off these produces nonsense
# ("angus" -> "angu"), which then leaks into what the coach calls the food.
_NOT_PLURAL_SUFFIXES = ("us", "ss", "is", "os")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if not unicodedata.combining(c))


def _drop_phrases(text: str, phrases: Iterable[str]) -> str:
    # Longest first, so "freshly squeezed" is removed before "fresh" can match
    # part of it; the lookarounds keep "fresh" from touching "freshly".
    for phrase in sorted(phrases, key=len, reverse=True):
        text = re.sub(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", " ", text)
    return text


def _singular(word: str) -> str:
    """Crude de-pluralisation — enough to fold "oats"/"oat" and "fries"/"fry"
    without a stemmer's surprises.

    Only the suffixes where "es" is really part of the plural are rewritten
    ("tomatoes", "sandwiches"); everything else just loses a trailing "s". A blanket
    "es" -> "" rule reads "vegetables" as "vegetabl", which then fails every keyword
    it should have matched.
    """
    if len(word) <= 3 or word.endswith(_NOT_PLURAL_SUFFIXES):
        return word
    for suffix, replacement in (("ies", "y"), ("oes", "o"), ("ses", "s"),
                                ("xes", "x"), ("ches", "ch"), ("shes", "sh"),
                                ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 2:
            return word[: -len(suffix)] + replacement
    return word


def is_fried(raw: str) -> bool:
    """Whether the raw logged name says this food was fried. Read before
    normalisation strips the method, because "how it was cooked" is the one
    preparation detail worth noticing."""
    text = strip_accents(str(raw or "")).lower()
    return any(marker in text for marker in _FRIED_MARKERS)


def normalize(raw: str) -> str:
    """The comparison key for a logged food name: accent-free, brand-free,
    method-free, singular, whitespace-collapsed."""
    text = strip_accents(str(raw or "")).lower()
    text = re.sub(r"\([^)]*\)", " ", text)          # "cod (bacalhau)" -> "cod"
    text = re.sub(r"[^a-z0-9%\s-]", " ", text)
    text = text.replace("-", " ")
    text = _drop_phrases(text, _BRANDS)
    text = _drop_phrases(text, _QUALIFIERS)
    words = [_singular(w) for w in text.split() if w and w not in _STOPWORDS]
    # A name made *entirely* of qualifiers ("mixed", "assorted") would normalise to
    # nothing; fall back to the raw name so it stays countable and visible.
    if not words:
        return re.sub(r"\s+", " ", strip_accents(str(raw or "")).lower()).strip()
    return " ".join(words)


# -- stage 2: aliases and groups -----------------------------------------------
# Written in whatever form reads clearly; both sides are normalised at import, so a
# key here is matched exactly the way a logged name is.
_RAW_ALIASES: Dict[str, str] = {
    # pt-PT -> en (the log mixes both, sometimes inside one meal)
    "arroz branco": "white rice",
    "arroz": "rice",
    "bife de vaca": "beef steak",
    "bife": "beef steak",
    "secreto de porco": "pork",
    "picanha": "beef steak",
    "chuleton rib steak": "beef steak",
    "chuleton": "beef steak",
    "sandes de frango assado": "chicken sandwich",
    "sandes de frango": "chicken sandwich",
    "sopa juliana": "vegetable soup",
    "sopa": "soup",
    "macedonia de vegetais": "vegetable medley",
    "mistura oriental de vegetais": "vegetable medley",
    "oriental vegetable mix": "vegetable medley",
    "garden vegetable macedoine": "vegetable medley",
    "vegetable macedoine": "vegetable medley",
    "migas": "bread mash",
    "queijo": "cheese",
    "pao": "bread",
    "peixe": "fish",
    "ovo": "egg",
    "frango": "chicken",
    "leite": "milk",
    "manteiga": "butter",
    "azeite": "olive oil",
    "batata": "potato",
    "feijao": "beans",
    "feijao preto": "black beans",
    "cenoura": "carrot",
    "alface": "lettuce",
    "tomate": "tomato",
    "maca": "apple",
    "laranja": "orange",
    "pera": "pear",
    "morango": "strawberry",
    "cerveja": "beer",
    "vinho": "wine",
    "vinho tinto": "red wine",
    "vinho branco": "white wine",
    # collapses: the same food logged under a different head noun
    "whey protein powder": "whey protein",
    "protein powder": "whey protein",
    "beef burger patty": "beef patty",
    "angus beef burger patty": "beef patty",
    "burger patty": "beef patty",
    "turkey breast cutlet": "turkey breast",
    "turkey steak": "turkey breast",
    "chicken breast fillet": "chicken breast",
    "chicken meat": "chicken",
    "white bread roll": "white bread",
    "bread roll": "white bread",
    "baguette bread": "baguette",
    "baguette roll": "baguette",
    "white baguette": "baguette",
    "orange slices": "orange",
    "green apple": "apple",
    "honeydew melon": "melon",
    "tiger shrimp": "shrimp",
    "pale lager beer": "beer",
    "lager beer": "beer",
    "lager": "beer",
    "roasted salted peanuts": "peanuts",
    "peanut": "peanuts",
    "cooking oil": "vegetable oil",
    "fries": "french fries",
    "spaghettoni": "spaghetti",
    "protein drink tropical": "protein drink",
    "bolacha protein drink": "protein drink",
    "acai base": "acai",
    "lemon ice tea": "iced tea",
    "ice tea": "iced tea",
    "blood sausage with rice": "blood sausage",
    "salmon nigiri": "salmon",
    "uramaki": "sushi roll",
    "sashimi": "raw fish",
    "almond and honey granola": "kefir granola",
    "liver pate": "pate",
    "garlic cream sauce": "cream sauce",
}

ALIASES: Dict[str, str] = {normalize(k): normalize(v)
                           for k, v in _RAW_ALIASES.items()
                           if normalize(k) != normalize(v)}


def canonical_name(raw: str) -> str:
    """The one name this food is counted under."""
    key = normalize(raw)
    for _ in range(4):                      # aliases may chain; never in a circle
        nxt = ALIASES.get(key)
        if not nxt or nxt == key:
            break
        key = nxt
    return key


# -- stage 2b: the pt-PT display lexicon ---------------------------------------
#
# The app is Portuguese (Portugal) end to end, but `name` is deliberately English:
# FDC is a USDA database, and every alias, group rule and aggregation key above is
# written against English. So a food carries TWO names — the English key it is
# counted under, and the pt-PT name a person actually reads.
#
# Meals logged from now on carry their own `name_pt`, written by the ingest model
# (which sees the photo AND the user's own Portuguese note, so "francesinha" stays
# "francesinha"). THIS table is the fallback for two cases that field cannot serve:
# the months of history logged before it existed, and the canonical vocabulary the
# coach reasons in — "chicken breast" is a bucket name, not something any model
# wrote, so it needs a translation of its own.
#
# Curated rather than generated because this vocabulary is small, closed and
# already half-written: the pt-PT half of `_RAW_ALIASES` and `_RAW_GROUP_RULES` is
# the same food list seen from the other side. What isn't here is learned once by
# `translate_unknown` and cached in the taxonomy blob, exactly like a group is.
_RAW_PT: Dict[str, str] = {
    # -- meat, fish, eggs
    "chicken": "frango", "chicken breast": "peito de frango",
    "chicken thigh": "coxa de frango", "chicken sandwich": "sandes de frango",
    "turkey": "peru", "turkey breast": "peito de peru", "duck": "pato",
    "beef": "carne de vaca", "beef steak": "bife de vaca", "beef patty": "hambúrguer de vaca",
    "veal": "vitela", "pork": "porco", "lamb": "borrego", "ribeye": "entrecosto",
    "steak": "bife", "burger": "hambúrguer", "patty": "hambúrguer",
    "mince": "carne picada", "red meat": "carne vermelha", "cured meat": "carne curada",
    "ham": "fiambre", "bacon": "bacon", "sausage": "salsicha",
    "blood sausage": "morcela", "chorizo": "chouriço", "salami": "salame",
    "pate": "paté", "frankfurter": "salsicha", "hot dog": "cachorro",
    "fish": "peixe", "raw fish": "peixe cru", "cod": "bacalhau", "salmon": "salmão",
    "tuna": "atum", "sardine": "sardinha", "mackerel": "cavala", "trout": "truta",
    "hake": "pescada", "haddock": "arinca", "herring": "arenque", "sole": "linguado",
    "sea bass": "robalo", "tilapia": "tilápia", "anchovy": "anchova",
    "shrimp": "camarão", "prawn": "gamba", "squid": "lula", "octopus": "polvo",
    "clam": "amêijoa", "mussel": "mexilhão", "crab": "caranguejo", "lobster": "lavagante",
    "seafood": "marisco", "sushi roll": "sushi", "egg": "ovo", "omelette": "omelete",
    # -- grains and bread
    "rice": "arroz", "white rice": "arroz branco", "brown rice": "arroz integral",
    "bread": "pão", "white bread": "pão branco", "wholemeal bread": "pão integral",
    "baguette": "baguete", "toast": "torrada", "roll": "papo-seco", "bun": "pão",
    "bread mash": "migas", "pasta": "massa", "spaghetti": "esparguete",
    "macaroni": "macarrão", "noodle": "noodles", "couscous": "cuscuz",
    "oats": "aveia", "quinoa": "quinoa", "buckwheat": "trigo-sarraceno",
    "bulgur": "bulgur", "rye": "centeio", "spelt": "espelta", "flour": "farinha",
    "cereal": "cereais", "granola": "granola", "muesli": "muesli",
    "kefir granola": "granola com kefir", "tortilla": "tortilha", "wrap": "wrap",
    "whole grain": "cereal integral", "cracker": "bolacha de água e sal",
    # -- vegetables and legumes
    "vegetable": "legumes", "vegetable medley": "macedónia de legumes",
    "vegetable soup": "sopa de legumes", "soup": "sopa", "salad": "salada",
    "potato": "batata", "sweet potato": "batata-doce", "french fries": "batatas fritas",
    "potato chips": "batatas fritas de pacote", "hash brown": "batata ralada frita",
    "carrot": "cenoura", "onion": "cebola", "tomato": "tomate", "lettuce": "alface",
    "cucumber": "pepino", "pepper": "pimento", "broccoli": "brócolos",
    "cauliflower": "couve-flor", "cabbage": "couve", "kale": "couve-galega",
    "spinach": "espinafres", "watercress": "agrião", "asparagus": "espargos",
    "courgette": "courgette", "zucchini": "courgette", "aubergine": "beringela",
    "mushroom": "cogumelos", "beetroot": "beterraba", "pea": "ervilhas",
    "green bean": "feijão-verde", "bean": "feijão", "beans": "feijão",
    "black beans": "feijão preto", "chickpea": "grão-de-bico", "lentil": "lentilhas",
    "edamame": "edamame", "hummus": "húmus", "tofu": "tofu", "yam": "inhame",
    # -- fruit
    "fruit": "fruta", "apple": "maçã", "pear": "pera", "orange": "laranja",
    "tangerine": "tangerina", "clementine": "clementina", "banana": "banana",
    "grape": "uvas", "strawberry": "morangos", "berry": "frutos vermelhos",
    "peach": "pêssego", "plum": "ameixa", "cherry": "cerejas", "fig": "figo",
    "melon": "melão", "watermelon": "melancia", "pineapple": "ananás",
    "mango": "manga", "kiwi": "kiwi", "avocado": "abacate", "coconut": "coco",
    "acai": "açaí", "lemon": "limão",
    # -- dairy
    "milk": "leite", "condensed milk": "leite condensado", "cheese": "queijo",
    "fresh cheese": "queijo fresco", "curd": "requeijão", "cottage cheese": "cottage cheese",
    "yogurt": "iogurte", "yoghurt": "iogurte", "greek yogurt": "iogurte grego",
    "skyr": "skyr", "quark": "quark", "kefir": "kefir", "cream": "natas",
    "cream sauce": "molho de natas", "butter": "manteiga", "margarine": "margarina",
    "ice cream": "gelado", "pudding": "pudim", "mozzarella": "mozzarella",
    "parmesan": "parmesão", "cheddar": "cheddar", "feta": "queijo feta",
    # -- fats, nuts, seeds
    "olive oil": "azeite", "vegetable oil": "óleo vegetal",
    "sunflower oil": "óleo de girassol", "coconut oil": "óleo de coco",
    "avocado oil": "óleo de abacate", "oil": "óleo", "lard": "banha",
    "peanuts": "amendoins", "peanut butter": "manteiga de amendoim",
    "almond": "amêndoas", "walnut": "nozes", "hazelnut": "avelãs",
    "cashew": "cajus", "pistachio": "pistácios", "nut butter": "manteiga de frutos secos",
    "seed": "sementes", "chia": "chia", "flax": "linhaça", "tahini": "tahini",
    "mayonnaise": "maionese",
    # -- sweets, snacks, drinks
    "sugar": "açúcar", "honey": "mel", "jam": "compota", "chocolate": "chocolate",
    "cake": "bolo", "cookie": "bolacha", "biscuit": "bolacha", "pastry": "pastel",
    "croissant": "croissant", "donut": "donut", "brownie": "brownie",
    "waffle": "waffle", "candy": "rebuçados", "dessert": "sobremesa",
    "popcorn": "pipocas", "pretzel": "pretzel", "nacho": "nachos", "crisps": "batatas fritas",
    "chips": "batatas fritas", "snack": "snack", "syrup": "xarope",
    "water": "água", "coffee": "café", "tea": "chá", "iced tea": "chá gelado",
    "infusion": "infusão", "juice": "sumo", "soda": "refrigerante", "cola": "cola",
    "energy drink": "bebida energética", "smoothie": "smoothie",
    "milkshake": "batido", "beer": "cerveja", "wine": "vinho",
    "red wine": "vinho tinto", "white wine": "vinho branco", "port": "vinho do Porto",
    "sangria": "sangria", "cider": "sidra", "vodka": "vodka", "gin": "gin",
    "whisky": "whisky", "rum": "rum", "tequila": "tequila", "liqueur": "licor",
    "pizza": "pizza", "sandwich": "sandes", "salt": "sal",
}

# Terms a Portuguese speaker uses in English anyway. Translating these mechanically
# ("proteína de soro de leite") reads worse than leaving them, so `display_pt`
# returns them untouched rather than reaching for the model.
_KEEP_AS_IS = ("whey protein", "protein powder", "protein shake", "protein bar",
               "protein drink", "protein ice cream", "casein", "creatine", "bcaa",
               "multivitamin", "supplement", "cottage cheese", "wrap", "smoothie",
               "granola", "muesli", "ketchup", "wasabi")

def display_key(raw: str) -> str:
    """The lexicon's key: case-, accent- and punctuation-insensitive, but otherwise
    the name AS LOGGED.

    Deliberately not `normalize`. That one exists to make "grilled chicken breast"
    and "boiled chicken breast" the same bucket — which is right for counting and
    wrong for showing, because it would also make them the same *label*. Keying the
    display lexicon here is what lets a learned entry carry "grelhado" while the
    grouping still counts both under one food.
    """
    text = strip_accents(str(raw or "")).lower()
    text = re.sub(r"[^a-z0-9%\s-]", " ", text).replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


PT_NAMES: Dict[str, str] = {}
for _k, _v in _RAW_PT.items():
    PT_NAMES.setdefault(display_key(_k), _v)
    PT_NAMES.setdefault(normalize(_k), _v)
_KEEP_KEYS = frozenset(
    k for term in _KEEP_AS_IS for k in (display_key(term), normalize(term)))


# Group rules, most specific first: (group, keywords). Keywords are normalised and
# matched as a whole token run inside the canonical name.
_RAW_GROUP_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    # -- supplements and drinks first: their names contain food words
    ("protein_supplement", ("whey", "protein powder", "protein drink", "casein",
                            "protein ice cream", "protein shake", "protein bar",
                            "creatine", "bcaa", "supplement", "multivitamin")),
    ("alcohol", ("beer", "wine", "vodka", "gin", "whisky", "rum", "cider",
                 "sangria", "liqueur", "tequila", "port")),
    ("sugary_drink", ("soda", "cola", "refrigerante", "sumo", "juice", "iced tea",
                      "energy drink", "milkshake", "smoothie")),
    ("drink_free", ("water", "agua", "coffee", "cafe", "tea", "cha", "infusion")),
    # -- animal protein: the red / processed / poultry / fish split
    ("processed_meat", ("ham", "bacon", "sausage", "chourico", "salame", "salami",
                        "presunto", "pate", "hot dog", "frankfurter", "chorizo",
                        "linguica", "mortadela", "morcela", "blood sausage",
                        "farinheira", "alheira", "salpicao", "cured meat")),
    ("red_meat", ("beef", "pork", "veal", "lamb", "vaca", "porco", "steak",
                  "patty", "burger", "mince", "ribeye", "picanha", "leitao",
                  "entrecosto", "costeleta", "red meat")),
    ("poultry", ("chicken", "turkey", "frango", "peru", "duck", "pato")),
    ("fish_oily", ("salmon", "salmao", "sardine", "sardinha", "mackerel", "cavala",
                   "trout", "truta", "tuna", "atum", "herring", "anchovy",
                   "sushi roll")),
    ("fish_white", ("cod", "bacalhau", "hake", "pescada", "sole", "linguado",
                    "dourada", "robalo", "sea bass", "tilapia", "haddock",
                    "fish")),
    ("seafood", ("shrimp", "camarao", "prawn", "squid", "lula", "polvo", "octopus",
                 "clam", "ameijoa", "mussel", "mexilhao", "crab", "lobster",
                 "seafood")),
    ("egg", ("egg", "ovo", "omelette", "omelete")),
    # -- plants
    ("legume", ("bean", "feijao", "chickpea", "grao de bico", "lentil", "lentilha",
                "pea", "ervilha", "soy", "soja", "tofu", "edamame", "hummus")),
    ("nut_seed", ("almond", "amendoa", "walnut", "noz", "cashew", "caju", "peanut",
                  "peanuts", "amendoim", "pistachio", "hazelnut", "avela", "seed",
                  "semente", "chia", "flax", "linhaca", "tahini", "nut butter")),
    ("vegetable", ("broccoli", "brocolo", "spinach", "espinafre", "lettuce",
                   "alface", "salad", "salada", "carrot", "cenoura", "tomato",
                   "tomate", "courgette", "zucchini", "onion", "cebola", "pepper",
                   "pimento", "cabbage", "couve", "kale", "grelos", "green bean",
                   "feijao verde", "cucumber", "pepino", "mushroom", "cogumelo",
                   "asparagus", "espargos", "beetroot", "beterraba", "aubergine",
                   "berinjela", "cauliflower", "couve flor", "vegetable",
                   "vegetais", "watercress", "agriao", "vegetable medley",
                   "vegetable soup", "soup")),
    ("fruit", ("apple", "maca", "banana", "orange", "laranja", "pear", "pera",
               "grape", "uva", "strawberry", "morango", "berry", "kiwi", "mango",
               "manga", "melon", "melao", "pineapple", "ananas", "peach",
               "pessego", "plum", "ameixa", "cherry", "cereja", "tangerine",
               "tangerina", "clementine", "fig", "figo", "acai", "avocado",
               "abacate", "fruit", "fruta")),
    # -- grains and starches: refined vs whole is the point of the split
    ("whole_grain", ("oats", "aveia", "wholemeal", "whole grain", "wholegrain",
                     "whole wheat", "integral", "brown rice", "arroz integral",
                     "quinoa", "buckwheat", "rye", "centeio", "spelt", "bulgur",
                     "granola", "muesli", "kefir granola")),
    ("fried_potato", ("french fries", "potato chips", "crisps", "batata frita",
                      "hash brown")),
    ("potato", ("potato", "batata", "sweet potato", "batata doce", "yam")),
    ("refined_grain", ("white rice", "rice", "arroz", "bread", "pao", "baguette",
                       "toast", "tosta", "pasta", "massa", "spaghetti",
                       "macaroni", "noodle", "couscous", "cereal", "cracker",
                       "bolacha", "tortilla", "wrap", "pizza", "flour", "farinha",
                       "sandwich", "sandes", "roll", "bun", "croissant",
                       "bread mash")),
    # -- dairy and fats
    ("cheese", ("cheese", "queijo", "requeijao", "mozzarella", "feta", "parmesan",
                "cheddar", "flamengo")),
    ("dairy_sweet", ("ice cream", "gelado", "pudding", "pudim",
                     "condensed milk")),
    ("dairy_plain", ("milk", "leite", "yogurt", "iogurte", "yoghurt", "skyr",
                     "kefir", "quark", "curd")),
    ("fat_healthy", ("olive oil", "azeite", "avocado oil", "nut oil",
                     "vegetable oil", "sunflower oil", "oleo", "oil")),
    ("fat_sat", ("butter", "manteiga", "cream", "nata", "lard", "banha",
                 "mayonnaise", "maionese", "margarine", "cream sauce",
                 "coconut oil")),
    # -- discretionary
    ("sweet", ("chocolate", "cake", "bolo", "cookie", "biscuit", "candy", "doce",
               "sugar", "acucar", "honey", "mel", "jam", "compota", "donut",
               "pastry", "pastel", "waffle", "brownie", "dessert", "sobremesa",
               "syrup", "nutella")),
    ("savory_snack", ("chips", "popcorn", "pipoca", "pretzel", "nacho", "snack")),
)

GROUP_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = tuple(
    (group, tuple(sorted({normalize(k) for k in keywords}, key=len, reverse=True)))
    for group, keywords in _RAW_GROUP_RULES
)

# What each group means for advice. `posture` is the direction a dietitian would
# push it; `week_min` / `week_max` are servings-per-week reference points from
# mainstream guidance. `serving_g` is what counts as one serving of it.
GROUP_INFO: Dict[str, Dict[str, Any]] = {
    "whole_grain":        {"label": "cereais integrais", "posture": "more", "week_min": 7, "serving_g": 60},
    "refined_grain":      {"label": "cereais refinados", "posture": "less", "week_max": 10, "serving_g": 60},
    "potato":             {"label": "batata", "posture": "neutral", "serving_g": 150},
    "fried_potato":       {"label": "fritos de batata", "posture": "less", "week_max": 1, "serving_g": 120},
    "red_meat":           {"label": "carne vermelha", "posture": "less", "week_max": 3, "serving_g": 100},
    "processed_meat":     {"label": "carne processada", "posture": "less", "week_max": 1, "serving_g": 40},
    "poultry":            {"label": "aves", "posture": "neutral", "serving_g": 120},
    "fish_oily":          {"label": "peixe gordo", "posture": "more", "week_min": 1, "serving_g": 120},
    "fish_white":         {"label": "peixe branco", "posture": "more", "week_min": 1, "serving_g": 130},
    "seafood":            {"label": "marisco", "posture": "neutral", "serving_g": 100},
    "egg":                {"label": "ovos", "posture": "neutral", "serving_g": 50},
    "legume":             {"label": "leguminosas", "posture": "more", "week_min": 3, "serving_g": 80},
    "nut_seed":           {"label": "frutos secos", "posture": "more", "week_min": 4, "serving_g": 25},
    "vegetable":          {"label": "legumes", "posture": "more", "week_min": 14, "serving_g": 80},
    "fruit":              {"label": "fruta", "posture": "more", "week_min": 10, "serving_g": 120},
    "dairy_plain":        {"label": "lácteos", "posture": "neutral", "serving_g": 200},
    "cheese":             {"label": "queijo", "posture": "neutral", "serving_g": 30},
    "dairy_sweet":        {"label": "lácteos doces", "posture": "less", "week_max": 3, "serving_g": 100},
    "fat_healthy":        {"label": "gorduras boas", "posture": "neutral", "serving_g": 10},
    "fat_sat":            {"label": "gorduras saturadas", "posture": "less", "week_max": 7, "serving_g": 10},
    "sweet":              {"label": "doces", "posture": "less", "week_max": 3, "serving_g": 40},
    "savory_snack":       {"label": "snacks salgados", "posture": "less", "week_max": 2, "serving_g": 30},
    "alcohol":            {"label": "álcool", "posture": "less", "week_max": 3, "serving_g": 250},
    "sugary_drink":       {"label": "bebidas açucaradas", "posture": "less", "week_max": 2, "serving_g": 250},
    "drink_free":         {"label": "bebidas sem açúcar", "posture": "neutral", "serving_g": 250},
    "protein_supplement": {"label": "suplementos de proteína", "posture": "neutral", "serving_g": 30},
    "other":              {"label": "outros", "posture": "neutral", "serving_g": 100},
}

# The groups that read as "a plant food" and as "a protein food" — used by the
# per-meal composition checks (a plate with neither is what's worth noticing).
PLANT_GROUPS = ("vegetable", "fruit", "legume", "nut_seed", "whole_grain")
PROTEIN_GROUPS = ("red_meat", "processed_meat", "poultry", "fish_oily",
                  "fish_white", "seafood", "egg", "legume", "dairy_plain",
                  "cheese", "protein_supplement")
FISH_GROUPS = ("fish_oily", "fish_white", "seafood")
ULTRA_PROCESSED_GROUPS = ("processed_meat", "sweet", "savory_snack", "dairy_sweet",
                          "sugary_drink", "fried_potato")


def group_by_rules(canonical: str) -> Optional[str]:
    """The group from the curated rules, or None if nothing matches (the only case
    worth spending a model call on)."""
    padded = f" {canonical} "
    for group, keywords in GROUP_RULES:
        for keyword in keywords:
            if f" {keyword} " in padded:
                return group
    return None


def servings(group: str, grams: float) -> float:
    """How many reference servings `grams` of a food in `group` is.

    Counting occurrences alone would read a 30 g sliver of cod and a 250 g fillet
    as the same "one fish meal"; counting grams alone would let a single big plate
    look like a week of vegetables. So a portion at least a third of a serving
    counts, and one occurrence is capped at two servings.
    """
    serving_g = float(GROUP_INFO.get(group, GROUP_INFO["other"]).get("serving_g") or 100)
    if grams <= 0 or serving_g <= 0:
        return 0.0
    raw = grams / serving_g
    if raw < 0.34:
        return 0.0
    return round(min(raw, 2.0), 2)


def label(group: str) -> str:
    return str(GROUP_INFO.get(group, GROUP_INFO["other"]).get("label") or group)


# -- stage 3: the learned taxonomy blob ----------------------------------------

# 2 added the `pt` map — the learned half of the pt-PT display lexicon.
TAXONOMY_VERSION = 2

_CLASSIFY_RULES = """Classificas alimentos de um registo alimentar português. Para cada
nome dado, devolve o alimento canónico (nome curto, sem marca nem modo de preparação) e
o GRUPO a que pertence, escolhido EXCLUSIVAMENTE desta lista:

{groups}

Regras:
- Se o nome for uma marca ou produto comercial, usa o alimento real que ele é.
- Se for um prato composto, escolhe o grupo do ingrediente dominante.
- "processed_meat" é carne curada/transformada (fiambre, chouriço, morcela, paté).
- "refined_grain" é cereal refinado (pão branco, arroz branco, massa); "whole_grain" só
  se for integral/aveia/quinoa.
- Se não conseguires classificar, usa "other". Nunca inventes um grupo novo.

Devolve APENAS: {{"foods": [{{"name": "<o nome dado>", "canonical": "...", "group": "..."}}]}}"""


def build_classify_prompt(names: List[str]) -> str:
    groups = ", ".join(sorted(GROUP_INFO))
    listed = "\n".join(f"- {n}" for n in names)
    return (_CLASSIFY_RULES.format(groups=groups)
            + f"\n\nNOMES A CLASSIFICAR:\n{listed}")


def empty_taxonomy() -> Dict[str, Any]:
    return {"version": TAXONOMY_VERSION, "foods": {}, "pt": {}}


def display_pt(raw: str, taxonomy: Optional[Dict[str, Any]] = None, *,
               name_pt: Optional[str] = None) -> str:
    """The pt-PT name to SHOW for a logged food. Never raises, never returns empty:
    an unknown food falls back to its own English name, because a meal the user can
    read in the wrong language beats a meal that isn't there.

    Resolution runs most-specific first. The meal's own `name_pt` wins outright —
    it was written against the actual photo and note. Then the exact logged spelling
    (so "grilled chicken breast" can keep its "grelhado"), and only then the
    canonical bucket, which by construction has lost the qualifiers.
    """
    raw = str(raw or "").strip()
    if name_pt and str(name_pt).strip():
        return str(name_pt).strip()
    if not raw:
        return raw

    exact, key, canonical = display_key(raw), normalize(raw), canonical_name(raw)
    if exact in _KEEP_KEYS or key in _KEEP_KEYS:
        return raw
    learned = (taxonomy or {}).get("pt") or {}
    # Exact spelling first (it can carry the cooking method), then the bucket.
    for candidate in (learned.get(exact), PT_NAMES.get(exact),
                      learned.get(key), PT_NAMES.get(key),
                      learned.get(canonical), PT_NAMES.get(canonical)):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return raw


def lookup(taxonomy: Optional[Dict[str, Any]], raw: str) -> Dict[str, Any]:
    """Everything known about one logged food name: its canonical name, its group,
    where that group came from (`rule`, `llm` or `fallback`), and the pt-PT name to
    display it under.

    The learned blob is consulted only for names the rules can't place, so a stale
    or wrong model answer can never override the curated taxonomy.
    """
    canonical = canonical_name(raw)
    fried = is_fried(raw)
    # The display name is resolved from the RAW name, not the canonical one: the
    # canonical has had cooking method and qualifiers stripped for grouping, and
    # showing "frango" for what the user logged as grilled chicken breast throws
    # away detail the app is meant to show.
    pt = display_pt(raw, taxonomy)
    pt_canonical = display_pt(canonical, taxonomy)
    group = group_by_rules(canonical)
    if group:
        return {"canonical": canonical, "group": group, "source": "rule",
                "fried": fried, "pt": pt, "pt_canonical": pt_canonical}
    learned = (taxonomy or {}).get("foods", {}).get(canonical)
    if isinstance(learned, dict) and learned.get("group") in GROUP_INFO:
        resolved = str(learned.get("canonical") or canonical)
        return {"canonical": resolved, "group": str(learned["group"]),
                "source": "llm", "fried": fried, "pt": pt,
                "pt_canonical": display_pt(resolved, taxonomy)}
    return {"canonical": canonical, "group": "other", "source": "fallback",
            "fried": fried, "pt": pt, "pt_canonical": pt_canonical}


def unknown_names(taxonomy: Optional[Dict[str, Any]],
                  raws: Iterable[str]) -> List[str]:
    """Canonical names neither the rules nor the learned blob can place — the exact
    set worth one classification call."""
    out: List[str] = []
    for raw in raws:
        info = lookup(taxonomy, raw)
        if info["source"] == "fallback" and info["canonical"] not in out:
            out.append(info["canonical"])
    return out


def classify_unknown(taxonomy: Optional[Dict[str, Any]], raws: Iterable[str],
                     call: Callable[[str], Dict[str, Any]], *,
                     limit: int = 40) -> Tuple[Dict[str, Any], int]:
    """Ask the model to place the names nothing else could, and fold the answers
    into `taxonomy`. Returns (taxonomy, number learned).

    `call` takes a prompt and returns the parsed JSON object — injected so this is
    testable without a network and so the caller owns the model choice. A failure is
    swallowed: an unplaced food stays in `other` and the feed is still generated,
    which always beats no coach at all.
    """
    base = dict(taxonomy or empty_taxonomy())
    base.setdefault("version", TAXONOMY_VERSION)
    base.setdefault("foods", {})
    names = unknown_names(base, raws)[:limit]
    if not names:
        return base, 0
    try:
        answer = call(build_classify_prompt(names))
    except Exception as exc:
        log.warning("food classification failed (non-fatal): %s", exc)
        return base, 0

    foods = dict(base.get("foods") or {})
    learned = 0
    for entry in (answer.get("foods") or []):
        if not isinstance(entry, dict):
            continue
        key = normalize(str(entry.get("name") or ""))
        group = str(entry.get("group") or "").strip()
        if not key or group not in GROUP_INFO:
            continue
        foods[key] = {"canonical": normalize(str(entry.get("canonical") or key)) or key,
                      "group": group}
        learned += 1
    base["foods"] = foods
    return base, learned


# -- the learned half of the pt-PT lexicon -------------------------------------
#
# Same shape and same division of labour as `classify_unknown` above: the model
# only ever CONTRIBUTES to a reference table that the code then reads
# deterministically, so what the app displays never changes run to run.
#
# This call is self-extinguishing. Meals logged since `name_pt` exists carry their
# own display name and never reach here; what's left is the historical backlog and
# the coach's canonical buckets, both finite. Once they're learned the taxonomy has
# every name and this stops firing entirely.
_TRANSLATE_RULES = """Traduzes nomes de alimentos para português de Portugal (pt-PT),
para serem mostrados numa app de registo alimentar. Os nomes vêm em inglês (ou já em
português) de um registo alimentar.

Regras:
- Português EUROPEU, nunca do Brasil: "peito de frango" e não "peito de frango
  grelhadinho"; "sumo" e não "suco"; "casa de banho" e não "banheiro".
- Minúsculas, sem artigo à frente ("arroz branco", não "o arroz branco").
- Mantém EXATAMENTE o mesmo nível de detalhe do nome dado: "grilled chicken breast"
  -> "peito de frango grelhado" (não só "frango"); "skin-on chicken thigh" -> "coxa
  de frango com pele".
- Marcas, nomes próprios e termos que em Portugal se dizem em inglês ficam como
  estão: "whey protein", "Big Tasty", "cottage cheese", "wrap", "smoothie".
- Se o nome JÁ estiver em português, devolve-o tal como está.
- Nunca inventes um alimento diferente do que te é dado. Se não souberes traduzir,
  devolve o nome original.

Devolve APENAS: {{"foods": [{{"name": "<o nome dado>", "pt": "..."}}]}}"""


def build_translate_prompt(names: List[str]) -> str:
    listed = "\n".join(f"- {n}" for n in names)
    return f"{_TRANSLATE_RULES}\n\nNOMES A TRADUZIR:\n{listed}"


def untranslated_names(taxonomy: Optional[Dict[str, Any]],
                       raws: Iterable[str]) -> List[str]:
    """The logged spellings nothing can put a Portuguese name to yet — the exact set
    worth one translation call. Returns the ORIGINAL spelling (not the normalised
    key) because the model translates real words better than stemmed ones."""
    out: List[str] = []
    seen: set = set()
    for raw in raws:
        raw = str(raw or "").strip()
        if not raw:
            continue
        key = display_key(raw)
        # `display_pt` returns the input unchanged both when it found nothing AND
        # when the term is deliberately left in English, so the keep-list has to be
        # checked separately — otherwise "whey protein" would be re-sent for
        # translation on every single run, forever.
        if key in seen or key in _KEEP_KEYS or normalize(raw) in _KEEP_KEYS:
            continue
        if display_pt(raw, taxonomy) != raw:
            continue
        seen.add(key)
        out.append(raw)
    return out


def translate_unknown(taxonomy: Optional[Dict[str, Any]], raws: Iterable[str],
                      call: Callable[[str], Dict[str, Any]], *,
                      limit: int = 60) -> Tuple[Dict[str, Any], int]:
    """Learn the pt-PT name for everything the curated table can't place, and fold
    the answers into `taxonomy`. Returns (taxonomy, number learned).

    A failure is swallowed exactly as in `classify_unknown`: the food keeps showing
    its English name and the app still renders, which always beats an empty screen.
    """
    base = dict(taxonomy or empty_taxonomy())
    base.setdefault("version", TAXONOMY_VERSION)
    base.setdefault("foods", {})
    base.setdefault("pt", {})
    names = untranslated_names(base, raws)[:limit]
    if not names:
        return base, 0
    try:
        answer = call(build_translate_prompt(names))
    except Exception as exc:
        log.warning("food translation failed (non-fatal): %s", exc)
        return base, 0

    pt_map = dict(base.get("pt") or {})
    learned = 0
    for entry in (answer.get("foods") or []):
        if not isinstance(entry, dict):
            continue
        key = display_key(str(entry.get("name") or ""))
        pt = str(entry.get("pt") or "").strip()[:120]
        # An answer for a name we never asked about is a hallucinated key that
        # would sit in the lexicon forever — drop it rather than store it.
        if not key or not pt or key not in {display_key(n) for n in names}:
            continue
        pt_map[key] = pt
        learned += 1
    base["pt"] = pt_map
    return base, learned
