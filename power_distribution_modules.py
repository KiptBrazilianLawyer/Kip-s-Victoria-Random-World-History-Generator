from __future__ import annotations

import math
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


STRATEGIC_WEIGHTS = {
    "building_coal_mine": 1.25,
    "building_iron_mine": 1.25,
    "building_lead_mine": 1.00,
    "building_sulfur_mine": 1.00,
    "building_oil_rig": 1.60,
    "building_rubber_plantation": 1.25,
    "building_gold_field": 1.35,
    "building_gold_mine": 1.35,
    "building_logging_camp": 0.45,
    "building_fishing_wharf": 0.35,
    "building_whaling_station": 0.30,
}

POWER_MODE_LABELS = {
    "natural": "Natural, sem regiões historicamente favorecidas",
    "balanced_continents": "Potenciais de potência equilibrados entre grandes zonas do mundo",
    "regional_random": "Algumas potências regionais sorteadas pela seed",
    "global_random": "Poucas grandes potências mundiais sorteadas pela seed",
    "keep_base": "Manter estratégias regionais especiais do mod-base",
}

MACRO_GROUPS = {
    "00_west_europe": "Europa",
    "01_south_europe": "Europa",
    "02_east_europe": "Europa",
    "15_russia": "Eurásia setentrional",
    "03_north_africa": "África",
    "04_subsaharan_africa": "África",
    "05_north_america": "América do Norte",
    "06_central_america": "América do Norte",
    "07_south_america": "América do Sul",
    "08_middle_east": "Ásia ocidental e central",
    "09_central_asia": "Ásia ocidental e central",
    "10_india": "Ásia meridional",
    "11_east_asia": "Ásia oriental",
    "12_indonesia": "Sudeste Asiático",
    "13_australasia": "Oceania",
    "14_siberia": "Eurásia setentrional",
}


@dataclass
class RegionPowerMetrics:
    region: str
    display_name: str
    macro_group: str
    states: List[str]
    population: int = 0
    arable: int = 0
    visible_resources: float = 0.0
    hidden_resources: float = 0.0
    resource_diversity: int = 0
    coastal_states: int = 0
    centers: int = 0
    score: float = 0.0
    anchor_state: str = ""


@dataclass
class PowerCandidate:
    region: str
    state: str
    macro_group: str
    score: float
    modifier: str


@dataclass
class PowerDistributionPlan:
    mode: str
    metrics: Dict[str, RegionPowerMetrics]
    candidates: List[PowerCandidate] = field(default_factory=list)
    removed_british_loops: int = 0
    removed_historical_strategy_file: bool = False
    warnings: List[str] = field(default_factory=list)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _write_text(path: Path, text: str, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if bom:
        text = "\ufeff" + text.lstrip("\ufeff")
    path.write_bytes(text.encode("utf-8"))


def _brace_balance(text: str) -> int:
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    for char in text:
        if in_comment:
            if char == "\n":
                in_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == "#":
            in_comment = True
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("Balanço de chaves negativo")
    return depth


def _matching_brace(text: str, open_idx: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    for index in range(open_idx, len(text)):
        char = text[index]
        if in_comment:
            if char == "\n":
                in_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == "#":
            in_comment = True
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError(f"Chave sem fechamento em {open_idx}")


def _prettify_key(key: str) -> str:
    value = key
    for prefix in ("STATE_", "region_", "geographic_region_"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value.replace("_old", "").replace("_", " ").strip().title()


def _load_localization(base: Path, language: str = "braz_por") -> Dict[str, str]:
    result: Dict[str, str] = {}
    root = base / "localization" / language
    if not root.exists():
        return result
    rx = re.compile(r'^\s*([A-Za-z0-9_.:-]+):\d*\s+"((?:[^"\\]|\\.)*)"')
    for path in sorted(root.rglob("*.yml")):
        try:
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            match = rx.match(line)
            if not match:
                continue
            raw = match.group(2)
            raw = raw.replace('\\"', '"').replace('\\n', ' ')
            result.setdefault(match.group(1), raw)
    return result


def _state_visible_hidden(state: object) -> Tuple[float, float, Set[str]]:
    visible = 0.0
    hidden = 0.0
    kinds: Set[str] = set()
    capped = getattr(state, "new_capped", {}) or {}
    for building, amount in capped.items():
        weight = STRATEGIC_WEIGHTS.get(building, 0.55)
        visible += max(0, int(amount)) * weight
        if amount:
            kinds.add(building)
    resources = getattr(state, "new_resources", []) or []
    for record in resources:
        building = getattr(record, "building", "")
        weight = STRATEGIC_WEIGHTS.get(building, 0.55)
        discovered = max(0, int(getattr(record, "discovered_amount", 0) or 0))
        amount = max(0, int(getattr(record, "amount", 0) or 0))
        amount_key = getattr(record, "amount_key", "")
        visible += discovered * weight
        if amount_key == "undiscovered_amount":
            hidden += amount * weight
        else:
            visible += amount * weight
        if amount or discovered:
            kinds.add(building)
    return visible, hidden, kinds


def _normalise(values: Dict[str, float], log_scale: bool = False) -> Dict[str, float]:
    transformed = {
        key: math.log1p(max(0.0, value)) if log_scale else max(0.0, value)
        for key, value in values.items()
    }
    if not transformed:
        return {}
    lo = min(transformed.values())
    hi = max(transformed.values())
    if hi <= lo:
        return {key: 0.5 for key in transformed}
    return {key: (value - lo) / (hi - lo) for key, value in transformed.items()}


def _majority_macro(states: Sequence[str], state_map: Dict[str, object]) -> str:
    counts = Counter(MACRO_GROUPS.get(getattr(state_map[name], "macro", ""), "Outras regiões") for name in states if name in state_map)
    return counts.most_common(1)[0][0] if counts else "Outras regiões"


def _anchor_state(member_names: Sequence[str], states: Dict[str, object], centers: Dict[str, str]) -> str:
    best = ""
    best_score = -1.0
    for name in member_names:
        state = states.get(name)
        if state is None:
            continue
        visible, hidden, kinds = _state_visible_hidden(state)
        population = max(0, int(getattr(state, "target_population", 0) or 0))
        arable = max(0, int(getattr(state, "new_arable_land", 0) or 0))
        score = (
            math.log1p(population) * 3.0
            + math.log1p(arable) * 2.0
            + math.log1p(visible) * 2.2
            + math.log1p(hidden) * 0.8
            + len(kinds) * 0.45
            + (1.5 if getattr(state, "coastal", False) else 0.0)
            + (2.0 if name in centers else 0.0)
        )
        if score > best_score:
            best_score = score
            best = name
    return best or (member_names[0] if member_names else "")


def calculate_region_power_metrics(
    base: Path,
    states: Dict[str, object],
    strategic_plan: object,
    centers: Dict[str, str],
) -> Dict[str, RegionPowerMetrics]:
    localization = _load_localization(base)
    metrics: Dict[str, RegionPowerMetrics] = {}
    assignments = getattr(strategic_plan, "assignments", {})
    capital_states = getattr(strategic_plan, "capital_states", {})
    for region, members in assignments.items():
        visible = 0.0
        hidden = 0.0
        kinds: Set[str] = set()
        population = 0
        arable = 0
        coastal = 0
        center_count = 0
        for state_name in members:
            state = states[state_name]
            population += max(0, int(getattr(state, "target_population", 0) or 0))
            arable += max(0, int(getattr(state, "new_arable_land", 0) or 0))
            state_visible, state_hidden, state_kinds = _state_visible_hidden(state)
            visible += state_visible
            hidden += state_hidden
            kinds.update(state_kinds)
            coastal += int(bool(getattr(state, "coastal", False)))
            center_count += int(state_name in centers)
        capital_state = capital_states.get(region, "") or _anchor_state(members, states, centers)
        display_name = localization.get(capital_state, "") or _prettify_key(capital_state or region)
        metrics[region] = RegionPowerMetrics(
            region=region,
            display_name=display_name,
            macro_group=_majority_macro(members, states),
            states=list(members),
            population=population,
            arable=arable,
            visible_resources=visible,
            hidden_resources=hidden,
            resource_diversity=len(kinds),
            coastal_states=coastal,
            centers=center_count,
            anchor_state=_anchor_state(members, states, centers),
        )

    pop_n = _normalise({key: item.population for key, item in metrics.items()}, log_scale=True)
    arable_n = _normalise({key: item.arable for key, item in metrics.items()}, log_scale=True)
    visible_n = _normalise({key: item.visible_resources for key, item in metrics.items()}, log_scale=True)
    hidden_n = _normalise({key: item.hidden_resources for key, item in metrics.items()}, log_scale=True)
    diversity_n = _normalise({key: item.resource_diversity for key, item in metrics.items()})
    coast_n = {
        key: item.coastal_states / max(1, len(item.states))
        for key, item in metrics.items()
    }
    center_n = _normalise({key: item.centers for key, item in metrics.items()})
    for key, item in metrics.items():
        item.score = round(100.0 * (
            0.30 * pop_n[key]
            + 0.18 * arable_n[key]
            + 0.22 * visible_n[key]
            + 0.12 * hidden_n[key]
            + 0.06 * diversity_n[key]
            + 0.07 * coast_n[key]
            + 0.05 * center_n[key]
        ), 2)
    return metrics


def _weighted_pick_without_replacement(
    rng: random.Random,
    candidates: Sequence[RegionPowerMetrics],
    count: int,
    max_per_macro: int,
) -> List[RegionPowerMetrics]:
    pool = list(candidates)
    selected: List[RegionPowerMetrics] = []
    macro_counts: Counter = Counter()
    while pool and len(selected) < count:
        eligible = [item for item in pool if macro_counts[item.macro_group] < max_per_macro]
        if not eligible:
            break
        weights = [(item.score + 12.0) ** 1.15 for item in eligible]
        total = sum(weights)
        point = rng.random() * total
        cumulative = 0.0
        chosen = eligible[-1]
        for item, weight in zip(eligible, weights):
            cumulative += weight
            if point <= cumulative:
                chosen = item
                break
        selected.append(chosen)
        macro_counts[chosen.macro_group] += 1
        pool.remove(chosen)
    return selected


def build_power_distribution_plan(
    base: Path,
    states: Dict[str, object],
    strategic_plan: object,
    centers: Dict[str, str],
    mode: str,
    seed: int,
) -> PowerDistributionPlan:
    if mode not in POWER_MODE_LABELS:
        raise ValueError(f"Modo de distribuição de potência inválido: {mode}")
    metrics = calculate_region_power_metrics(base, states, strategic_plan, centers)
    plan = PowerDistributionPlan(mode=mode, metrics=metrics)
    if mode in {"natural", "keep_base"}:
        return plan

    rng = random.Random(f"bwg-v6.8-power:{seed}:{mode}")
    values = list(metrics.values())
    chosen: List[RegionPowerMetrics] = []
    if mode == "balanced_continents":
        grouped: Dict[str, List[RegionPowerMetrics]] = defaultdict(list)
        for item in values:
            grouped[item.macro_group].append(item)
        for macro in sorted(grouped):
            # Each major zone receives one plausible center, but the exact region
            # still changes with the seed among the three strongest candidates.
            shortlist = sorted(grouped[macro], key=lambda item: item.score, reverse=True)[:3]
            if shortlist:
                weights = [max(1.0, item.score + 10.0) for item in shortlist]
                chosen.append(rng.choices(shortlist, weights=weights, k=1)[0])
        modifier = "bwg_power_candidate_balanced"
    elif mode == "regional_random":
        chosen = _weighted_pick_without_replacement(rng, values, min(8, len(values)), 2)
        modifier = "bwg_power_candidate_regional"
    else:  # global_random
        chosen = _weighted_pick_without_replacement(rng, values, min(4, len(values)), 1)
        if len(chosen) < min(4, len(values)):
            remaining = [item for item in values if item not in chosen]
            chosen.extend(_weighted_pick_without_replacement(rng, remaining, min(4, len(values)) - len(chosen), 2))
        modifier = "bwg_power_candidate_global"

    seen_states: Set[str] = set()
    for item in chosen:
        if not item.anchor_state or item.anchor_state in seen_states:
            continue
        seen_states.add(item.anchor_state)
        plan.candidates.append(PowerCandidate(
            region=item.region,
            state=item.anchor_state,
            macro_group=item.macro_group,
            score=item.score,
            modifier=modifier,
        ))
    return plan


def _remove_british_special_spawn_loops(path: Path) -> int:
    text = _read_text(path)
    matches = list(re.finditer(r"(?m)^([ \t]*)while\s*=\s*\{", text))
    candidates: List[Tuple[int, int, str]] = []
    for match in matches:
        op = text.find("{", match.start(), match.end())
        cl = _matching_brace(text, op)
        block = text[match.start():cl + 1]
        if "create_dynamic_country" not in block:
            continue
        refs = set(re.findall(r"is_in_geographic_region\s*=\s*([a-zA-Z0-9_]+)", block))
        british = {
            "geographic_region_england_old",
            "geographic_region_north_sea_coast_old",
        }
        if refs and refs.issubset(british):
            candidates.append((match.start(), cl + 1, block))
    # Only innermost matching loops are removed.
    operations = [
        item for item in candidates
        if not any(item[0] < other[0] and other[1] < item[1] for other in candidates)
    ]
    for start, end, _block in sorted(operations, reverse=True):
        while end < len(text) and text[end] in "\r\n":
            end += 1
        text = text[:start] + text[end:]
    if _brace_balance(text) != 0:
        raise ValueError("A remoção do laço britânico deixou o script inválido")
    _write_text(path, text, bom=True)
    return len(operations)


def _patch_power_on_action(mod_root: Path) -> None:
    path = mod_root / "common/on_actions/01_random_stuff.txt"
    text = _read_text(path)
    marker = "remove_all_buildings = {"
    start = text.find(marker)
    if start == -1:
        raise ValueError("Não encontrei remove_all_buildings para aplicar distribuição de potência")
    op = text.find("{", start)
    cl = _matching_brace(text, op)
    block = text[start:cl + 1]
    if "bwg_apply_power_distribution = yes" not in block:
        block_cl = _matching_brace(block, block.find("{"))
        block = block[:block_cl] + "\n\tbwg_apply_power_distribution = yes\n" + block[block_cl:]
        text = text[:start] + block + text[cl + 1:]
    if _brace_balance(text) != 0:
        raise ValueError("On-action de distribuição de potência ficou inválido")
    _write_text(path, text, bom=True)


def _write_power_files(mod_root: Path, plan: PowerDistributionPlan) -> None:
    static_text = """# Generated by Randomised World v6.8
bwg_power_candidate_balanced = {
    icon = gfx/interface/icons/timed_modifier_icons/modifier_statue_positive.dds
    country_prestige_mult = 0.05
    country_weekly_innovation_add = 2
    country_bureaucracy_add = 50
}

bwg_power_candidate_regional = {
    icon = gfx/interface/icons/timed_modifier_icons/modifier_statue_positive.dds
    country_prestige_mult = 0.08
    country_weekly_innovation_add = 3
    country_bureaucracy_add = 75
    country_influence_add = 25
}

bwg_power_candidate_global = {
    icon = gfx/interface/icons/timed_modifier_icons/modifier_statue_positive.dds
    country_prestige_mult = 0.12
    country_weekly_innovation_add = 5
    country_bureaucracy_add = 100
    country_influence_add = 50
}
"""
    _write_text(mod_root / "common/static_modifiers/99_bwg_power_distribution.txt", static_text, bom=True)

    lines = [
        "# Generated by Randomised World v6.8",
        "bwg_apply_power_distribution = {",
    ]
    if not plan.candidates:
        lines.append("    # Natural mode: no artificial power candidate is granted.")
    for candidate in plan.candidates:
        lines.extend([
            "    random_country = {",
            "        limit = {",
            "            NOT = { is_country_type = decentralized }",
            "            NOT = { has_variable = bwg_power_candidate_applied }",
            f"            any_scope_state = {{ state_region = s:{candidate.state} }}",
            "        }",
            f"        add_modifier = {{ name = {candidate.modifier} months = 240 }}",
            "        set_variable = bwg_power_candidate_applied",
            "    }",
        ])
    lines.extend(["}", ""])
    script = "\n".join(lines)
    if _brace_balance(script) != 0:
        raise ValueError("Script de distribuição de potência ficou inválido")
    _write_text(mod_root / "common/scripted_effects/98_bwg_power_distribution.txt", script, bom=True)

    loc = """l_braz_por:
 bwg_power_candidate_balanced:0 \"Centro regional promissor\"
 bwg_power_candidate_balanced_desc:0 \"Esta nação ocupa um dos polos mais promissores de sua grande zona geográfica. O impulso é moderado e temporário.\"
 bwg_power_candidate_regional:0 \"Potência regional emergente\"
 bwg_power_candidate_regional_desc:0 \"Condições econômicas e institucionais favorecem uma ascensão regional durante as primeiras décadas.\"
 bwg_power_candidate_global:0 \"Potência mundial emergente\"
 bwg_power_candidate_global_desc:0 \"Esta nação foi sorteada como um dos raros polos globais de ascensão desta seed.\"
"""
    _write_text(mod_root / "localization/braz_por/bwg_power_distribution_l_braz_por.yml", loc, bom=True)
    loc_en = """l_english:
 bwg_power_candidate_balanced:0 \"Promising regional center\"
 bwg_power_candidate_balanced_desc:0 \"This country occupies one of the most promising centers of its broad world zone. The boost is moderate and temporary.\"
 bwg_power_candidate_regional:0 \"Emerging regional power\"
 bwg_power_candidate_regional_desc:0 \"Economic and institutional conditions favor regional ascent during the first decades.\"
 bwg_power_candidate_global:0 \"Emerging world power\"
 bwg_power_candidate_global_desc:0 \"This country was selected as one of the few global centers of ascent in this seed.\"
"""
    _write_text(mod_root / "localization/english/bwg_power_distribution_l_english.yml", loc_en, bom=True)
    _patch_power_on_action(mod_root)


def apply_power_distribution_plan(mod_root: Path, plan: PowerDistributionPlan) -> None:
    if plan.mode != "keep_base":
        plan.removed_british_loops = _remove_british_special_spawn_loops(
            mod_root / "common/scripted_effects/02_random_stuff.txt"
        )
        strategy_path = mod_root / "common/ai_strategies/06_unify_ai_strategy_balanced.txt"
        if strategy_path.exists():
            strategy_path.unlink()
            plan.removed_historical_strategy_file = True
    if plan.mode != "keep_base":
        _write_power_files(mod_root, plan)


def validate_power_distribution(mod_root: Path, plan: PowerDistributionPlan) -> List[str]:
    messages: List[str] = []
    if plan.mode != "keep_base":
        fx = _read_text(mod_root / "common/scripted_effects/02_random_stuff.txt")
        for match in re.finditer(r"(?m)^([ \t]*)while\s*=\s*\{", fx):
            op = fx.find("{", match.start(), match.end())
            cl = _matching_brace(fx, op)
            block = fx[match.start():cl + 1]
            refs = set(re.findall(r"is_in_geographic_region\s*=\s*([a-zA-Z0-9_]+)", block))
            british = {"geographic_region_england_old", "geographic_region_north_sea_coast_old"}
            if "create_dynamic_country" in block and refs and refs.issubset(british):
                raise ValueError("O laço exclusivo de fragmentação britânica ainda está presente")
        if (mod_root / "common/ai_strategies/06_unify_ai_strategy_balanced.txt").exists():
            raise ValueError("As estratégias históricas regionais ainda estão ativas")
        power_script = mod_root / "common/scripted_effects/98_bwg_power_distribution.txt"
        if not power_script.exists():
            raise ValueError("Script de distribuição de potência não foi criado")
        if _brace_balance(_read_text(power_script)) != 0:
            raise ValueError("Script de distribuição de potência tem chaves inválidas")
        messages.append(
            f"Potência inicial: vieses regionais históricos removidos; {plan.removed_british_loops} laço(s) britânico(s) especial(is) eliminado(s)"
        )
        if plan.candidates:
            messages.append(
                f"Candidatos de potência da seed: {len(plan.candidates)} polos distribuídos em {len(set(c.macro_group for c in plan.candidates))} grandes zonas"
            )
        else:
            messages.append("Candidatos de potência: nenhum bônus artificial; ascensão depende apenas do mundo gerado")
    else:
        messages.append("Potência inicial: estratégias regionais especiais do mod-base mantidas")
    return messages


def _share(items: Iterable[float], top_n: int = 5) -> float:
    values = sorted((max(0.0, float(value)) for value in items), reverse=True)
    total = sum(values)
    return 0.0 if total <= 0 else 100.0 * sum(values[:top_n]) / total


def _fmt_int(value: float) -> str:
    return f"{int(round(value)):,}".replace(",", ".")


def _context_sentence(metrics: Dict[str, RegionPowerMetrics], attribute: str, role: str) -> str:
    item = max(metrics.values(), key=lambda metric: getattr(metric, attribute))
    value = getattr(item, attribute)
    if attribute in {"visible_resources", "hidden_resources"}:
        value_text = f"índice ponderado {_fmt_int(value)}"
    elif attribute == "coastal_states":
        value_text = f"{int(value)} estados costeiros"
    else:
        value_text = _fmt_int(value)
    return f"- {item.display_name} surge como {role} ({value_text})."


def build_seed_preview_report(
    states: Dict[str, object],
    options: Dict[str, str],
    seed: int,
    strategic_plan: object,
    centers: Dict[str, str],
    power_plan: PowerDistributionPlan,
    country_scale_stats: Dict[str, object],
    warnings: Sequence[str],
    validation: Sequence[str],
) -> str:
    metrics = power_plan.metrics
    ranked = sorted(metrics.values(), key=lambda item: item.score, reverse=True)
    pop_share = _share(item.population for item in metrics.values())
    arable_share = _share(item.arable for item in metrics.values())
    resource_share = _share(item.visible_resources for item in metrics.values())
    hidden_share = _share(item.hidden_resources for item in metrics.values())

    tiny_states = [s for s in states.values() if int(getattr(s, "province_count", 0)) <= 3]
    max_tiny_population = max((int(getattr(s, "target_population", 0)) for s in tiny_states), default=0)
    projected_infra_risks = []
    for state in states.values():
        population = int(getattr(state, "target_population", 0))
        arable = int(getattr(state, "new_arable_land", 0))
        # Projection only: actual infrastructure is finalized by the game engine.
        estimated_base = max(10.0, population / 20000.0)
        subsistence_load = arable * 0.2
        if subsistence_load > estimated_base * 0.90:
            projected_infra_risks.append((subsistence_load / estimated_base, getattr(state, "name", "STATE_UNKNOWN")))

    concentration_notes: List[str] = []
    if pop_share >= 45:
        concentration_notes.append("A população está relativamente concentrada em poucos polos.")
    else:
        concentration_notes.append("A população está distribuída sem um núcleo mundial excessivamente dominante.")
    if resource_share >= 50:
        concentration_notes.append("Os recursos inicialmente visíveis estão concentrados; comércio e disputas territoriais devem ser importantes.")
    else:
        concentration_notes.append("Os recursos visíveis estão razoavelmente espalhados entre diferentes regiões.")
    if hidden_share >= 55:
        concentration_notes.append("As reservas ocultas favorecem poucas fronteiras futuras, que podem mudar o equilíbrio ao longo da campanha.")
    else:
        concentration_notes.append("As reservas ocultas estão distribuídas entre diversos futuros polos de crescimento.")

    overseas_labels = {
        "none": "sem territórios ultramarinos procedurais",
        "rare_colonial": "raros domínios coloniais compactos",
        "few_colonial": "poucos domínios coloniais compactos",
        "original": "lógica ultramarina original",
    }
    subject_labels = {
        "none": "sem súditos procedurais",
        "very_rare": "vassalos excepcionalmente raros e vizinhos",
        "rare": "poucos vassalos plausíveis",
        "original": "lógica de súditos original",
    }
    fiscal_labels = {
        "strict": "estabilidade máxima — estrutura pública mínima e forças limitadas pela aptidão econômica",
        "balanced": "balanceada — infraestrutura moderada e forças condicionadas à economia",
        "legacy": "legado expansivo — maior risco de déficit inicial",
    }
    military_labels = {
        "none": "nenhuma força militar inicial",
        "economic_conservative": "avaliação econômica conservadora — exército e marinha raros",
        "economic_balanced": "avaliação econômica balanceada",
        "economic_strong": "avaliação econômica permissiva",
    }

    lines = [
        "PANORAMA DA SEED — PRÉVIA ANTES DE SALVAR",
        f"Seed: {seed}",
        "",
        "IMPORTANTE",
        "Este panorama descreve exatamente a geografia estratégica, população, agricultura, recursos, centros e polos de potência escritos pelo gerador.",
        "Os países dinâmicos e seus proprietários finais ainda são formados pelo motor do Victoria 3 ao iniciar a campanha; portanto, a quantidade final de países é uma projeção baseada no script, não uma captura do mapa já inicializado.",
        "",
        "DISTRIBUIÇÃO INICIAL DE POTÊNCIA",
        f"Modo: {POWER_MODE_LABELS.get(power_plan.mode, power_plan.mode)}",
    ]
    if power_plan.mode != "keep_base":
        lines.append("- As estratégias históricas fixas de reunificação foram removidas, inclusive o impulso especial das Ilhas Britânicas.")
        lines.append(f"- Laços exclusivos de fragmentação britânica removidos: {power_plan.removed_british_loops}.")
    else:
        lines.append("- As estratégias especiais do mod-base permanecem ativas; algumas regiões históricas podem voltar a dominar com frequência.")
    if power_plan.candidates:
        lines.append("- Polos temporariamente favorecidos nesta seed:")
        by_region = {item.region: item for item in metrics.values()}
        for candidate in power_plan.candidates:
            item = by_region[candidate.region]
            lines.append(
                f"  • {item.display_name} — {candidate.macro_group}; estado-âncora {_prettify_key(candidate.state)}; índice regional {candidate.score:.1f}/100"
            )
    else:
        lines.append("- Nenhum polo recebeu bônus artificial; as vantagens abaixo decorrem apenas da população, recursos e posição gerados.")

    lines.extend([
        "",
        "REGIÕES MAIS FAVORECIDAS PELO MUNDO GERADO",
    ])
    for index, item in enumerate(ranked[:10], start=1):
        lines.append(
            f"{index}. {item.display_name} — índice {item.score:.1f}/100; "
            f"população {_fmt_int(item.population)}; terra arável {_fmt_int(item.arable)}; "
            f"recursos visíveis {_fmt_int(item.visible_resources)}; reservas ocultas {_fmt_int(item.hidden_resources)}; "
            f"{item.coastal_states} estados costeiros; {item.resource_diversity} categorias de recursos."
        )

    lines.extend([
        "",
        "CONCENTRAÇÃO MUNDIAL",
        f"- As cinco regiões mais populosas concentram {pop_share:.1f}% da população.",
        f"- As cinco maiores bacias agrícolas concentram {arable_share:.1f}% da terra arável.",
        f"- As cinco regiões mais ricas concentram {resource_share:.1f}% dos recursos visíveis ponderados.",
        f"- As cinco maiores fronteiras futuras concentram {hidden_share:.1f}% das reservas ocultas ponderadas.",
        *[f"- {note}" for note in concentration_notes],
        "",
        "CONTEXTO HISTÓRICO PROCEDURAL",
        _context_sentence(metrics, "population", "o principal núcleo demográfico do mundo"),
        _context_sentence(metrics, "arable", "o grande celeiro potencial da era"),
        _context_sentence(metrics, "visible_resources", "a fronteira industrial mais pronta em 1836"),
        _context_sentence(metrics, "hidden_resources", "a região com maior potencial de transformação futura"),
        _context_sentence(metrics, "coastal_states", "o maior corredor marítimo e comercial"),
    ])
    if centers:
        center_counts = Counter(centers.values())
        center_names = {
            "knowledge": "conhecimento",
            "commerce": "comércio",
            "industry": "indústria",
            "military": "organização militar",
            "agriculture": "agricultura",
        }
        lines.append("- Os centros de civilização foram distribuídos assim: " + ", ".join(
            f"{center_names.get(key, key)}={value}" for key, value in sorted(center_counts.items())
        ) + ".")

    lines.extend([
        "",
        "AUDITORIA DE ESTABILIDADE PRÉ-JOGO",
        f"- Maior população projetada em estado de até três províncias: {_fmt_int(max_tiny_population)}.",
        f"- Estados em que só a terra de subsistência projetaria uso superior a 90% da infraestrutura-base estimada: {len(projected_infra_risks)}.",
        "- A estimativa acima não substitui o cálculo do motor, mas detecta novamente ilhas superpovoadas e terra arável incompatível antes de salvar.",
        "- Históricos de exércitos, marinhas e blocos de poder são removidos no mod gerado; despesas militares não devem ser herdadas das tags de 1836.",
        "",
        "PANORAMA POLÍTICO ESPERADO",
        f"- Escala territorial: {country_scale_stats.get('label', options.get('country_scale', 'n/a'))}.",
        f"- Tentativas procedurais de novos países: {country_scale_stats.get('new_min', '?')}–{country_scale_stats.get('new_max', '?')}; até {country_scale_stats.get('max_cluster', '?')} estados contíguos por novo país.",
        f"- Ultramar: {overseas_labels.get(options.get('overseas_territories', ''), options.get('overseas_territories', 'n/a'))}.",
        f"- Súditos: {subject_labels.get(options.get('subjects', ''), options.get('subjects', 'n/a'))}.",
        f"- Remanescentes históricos: {options.get('historical_remnants', 'dissolve')}.",
        f"- Diplomacia: {options.get('diplomacy', 'n/a')}.",
        f"- Segurança fiscal: {fiscal_labels.get(options.get('fiscal_safety', ''), options.get('fiscal_safety', 'n/a'))}.",
        f"- Forças iniciais: {military_labels.get(options.get('military_economy', ''), options.get('military_economy', 'n/a'))}.",
        "- Construções iniciais são limitadas aos estados incorporados do próprio país; colônias não incorporadas e territórios estrangeiros não recebem indústria automática.",
        "- O gerador remove as forças históricas e recalcula do zero: quartéis, administrações navais, estaleiros e navios só aparecem quando população, tecnologia, litoral e cadeias domésticas atingem os limites selecionados.",
        "- O vencedor de uma região compacta ainda pode crescer organicamente, mas nenhuma área recebe a antiga estratégia fixa que fazia a Grã-Bretanha se reunificar e dominar repetidamente.",
        "",
        "INTERPRETAÇÃO DA SEED",
    ])
    top = ranked[:4]
    if top:
        lines.append(
            "A ordem internacional tende a ser multipolar no início, com "
            + ", ".join(item.display_name for item in top[:-1])
            + (f" e {top[-1].display_name}" if len(top) > 1 else top[0].display_name)
            + " formando os polos materiais mais promissores."
        )
    lines.append(
        "Regiões com população, agricultura e minerais no mesmo espaço têm maior chance de industrialização precoce; regiões ricas apenas em reservas ocultas podem permanecer periféricas até que as descobertas alterem o equilíbrio."
    )
    lines.append(
        "Mercados que nascerem sem carvão, ferro, ferramentas ou alimento suficiente deverão depender de comércio, expansão ou investimento, criando uma história econômica diferente em cada inicialização."
    )

    if warnings:
        lines.extend(["", "AVISOS TÉCNICOS", *[f"- {warning}" for warning in warnings]])
    lines.extend([
        "",
        "VALIDAÇÕES PRINCIPAIS",
        *[f"- {item}" for item in validation[-12:]],
        "",
        "Escolha SALVAR MOD para aceitar esta seed ou CANCELAR para descartá-la e gerar outra.",
    ])
    return "\n".join(lines) + "\n"
