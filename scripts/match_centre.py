"""
Match Centre page (match_centre.html; predictions_matches.html forwards to it) - renderer used by match_predictor.py.

Lives next to match_predictor.py (v7 and the website's scripts/). match_predictor imports it only when it writes the
matches page; without this file it writes the old matches page (the app copies in streaming/ and content_studio/
don't need it).

Two inputs:
  * scripts/match_centre.json - made on Monday by match_centre_data.py (Control Panel step "Match Centre data"):
    board order, storyline chips, headlines, badges, squad links, who's on the boards, season so far.
    Used only when its season + round are the ones being predicted.
  * the live part from match_predictor: the round's games (results, game links), the match simulations (chances),
    what's at stake (--full-season run) and, when fetched, the league calendar (scheduled times).
Without the JSON the page still shows every match (teams, chances, boards, results, stakes) in the same look.
"""

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parent / 'match_centre.json'
POINTS = {'1-0': (1, 0), '0-1': (0, 1), '1/2-1/2': (.5, .5), '1X-0F': (1, 0), '0F-1X': (0, 1), '0F-0F': (0, 0),
          '1/2Z-1/2Z': (.5, .5)}
PLAYED = {'1-0', '0-1', '1/2-1/2'}
UNPLAYED_LABEL = {'1X-0F': 'forfeit', '0F-1X': 'forfeit', '0F-0F': 'double forfeit', '1/2Z-1/2Z': 'scheduling draw'}
BADGE_CLASS = {'Biggest swing': 'b-swing', 'Most connected': 'b-conn', 'Rematch night': 'b-rem',
               "Champions' match": 'b-champ', 'Milestones': 'b-mil', 'Debut day': 'b-debut',
               'Closest line-ups': 'b-close', 'Top of the table': 'b-top'}

E = lambda s: html.escape(str(s), quote=True)
L = lambda x: (x or '').lower()


# =============================================================================
# INPUTS
# =============================================================================

def load_data(season, round_num, path=None):
    """The Monday file when it is for this season + round, else None (with the reason printed)."""
    path = Path(path or DEFAULT_FILE)
    if not path.exists():
        print(f"  Match Centre: no {path.name} - page without the Monday part")
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        print(f"  Match Centre: {path.name} not readable ({e}) - page without the Monday part")
        return None
    if data.get('season') != season or data.get('round') != round_num or not isinstance(data.get('matches'), list):
        print(f"  Match Centre: {path.name} is for S{data.get('season')} R{data.get('round')}, not S{season} R{round_num} "
              f"- page without the Monday part")
        return None
    return data


def parse_calendar(text):
    """{frozenset(white, black) lowercase: start time (UTC)} from the league's calendar.ics text."""
    text = re.sub(r'\n+[ \t]', '', (text or '').replace('\r', '\n'))      # unfold long lines
    out = {}
    for block in text.split('BEGIN:VEVENT')[1:]:
        summ = re.search(r'SUMMARY:(.+)', block)
        start = re.search(r'DTSTART:(\d{8}T\d{6})Z?', block)
        if not summ or not start:
            continue
        summary = summ.group(1).strip().replace('\\,', ',').replace('\\;', ';').replace('\\\\', '\\')
        m = re.match(r'^(\S+)\s*\((.*)\)\s+vs\s+(\S+)\s*\((.*)\)\s*$', summary)
        if not m:
            continue
        t = datetime.strptime(start.group(1), '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc)
        out[frozenset((m.group(1).lower(), m.group(3).lower()))] = t
    return out


# =============================================================================
# SMALL HELPERS
# =============================================================================

def fmt_pct(p):
    """As match_predictor.fmt_pct: '–' never, '<1%' / '>99%' near the ends."""
    if p is None:
        return '–'
    if p <= 0:
        return '–'
    if p >= 1:
        return '100%'
    v = p * 100
    if v < 1:
        return '<1%'
    if v > 99:
        return '>99%'
    return f'{v:.0f}%'


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def half(x):
    w = int(x + 1e-9)
    f = x - w > 0.25
    return ('½' if f else '0') if w == 0 else f"{w}{'½' if f else ''}"


def plural(n, w, ws=None):
    return f"{n} {w if n == 1 else (ws or w + 's')}"


def team_eq(x, y):
    return ' '.join(str(x or '').split()).lower() == ' '.join(str(y or '').split()).lower()


def fmt_time(t):
    return t.strftime('%a %H:%M UTC')


def alt_html(tip):
    if not tip:
        return ''
    return f'<span class="altb" tabindex="0" title="{E(tip)}" data-tip="{E(tip)}">alt</span>'


# =============================================================================
# LIVE STATE OF ONE MATCH
# =============================================================================

def _board_state(g, calendar, now):
    """dict(state=done|playing|sched|late|unsched, ...) for one game of the round."""
    res = (g.get('result') or '').strip()
    gid = g.get('game_id')
    key = frozenset((L(g.get('white')), L(g.get('black'))))
    if res in POINTS:
        pw, pb = POINTS[res]
        return dict(state='done', pw=pw, pb=pb, kind=UNPLAYED_LABEL.get(res), gid=gid if res in PLAYED else None)
    t = (calendar or {}).get(key)
    if gid:                                   # the league links the game when it starts
        return dict(state='playing', t=t if t and t <= now else None, gid=gid)
    if t:
        return dict(state='sched', t=t) if t > now else dict(state='late', t=t)
    return dict(state='unsched')


def _rows_from_games(games, ta, ratings):
    """Board rows (no board numbers) straight from the season's games of a match."""
    rows = []
    for g in games:
        a_white = team_eq(g.get('white_team'), ta)
        pa, pb = (g['white'], g['black']) if a_white else (g['black'], g['white'])
        ra, rb = (g.get('white_rating'), g.get('black_rating')) if a_white else (g.get('black_rating'), g.get('white_rating'))
        rows.append(dict(board=None, a=pa, b=pb, a_white=a_white, ra=ratings.get(L(pa)) or ra, rb=ratings.get(L(pb)) or rb,
                         rank=None, chips=[], alt_a=None, alt_b=None, game=g))
    return rows


def _match_boards(cm, games, ratings, rosters, alt_boards):
    """Monday's boards (JSON) with this match's games now: same pairing = same board; a changed pairing goes to a
    free Monday board sharing a player, else one with the same colours."""
    by_pair = {frozenset((L(g['white']), L(g['black']))): g for g in games}
    rows, used = [], set()
    for b in cm['boards']:
        k = frozenset((L(b['a']), L(b['b'])))
        row = dict(b, changed=False, was=None, game=by_pair.get(k))
        if row['game']:
            used.add(k)
        rows.append(row)
    rest = [g for k, g in by_pair.items() if k not in used]

    def tip(name, team):
        names = rosters.get(team)
        if not names or name.lower() in names:
            return None
        b = alt_boards.get(name.lower())
        return f'Alternate for {team}' + (f' (listed as a Bd {b} alternate)' if b else '')

    def assign(row, g):
        a_white = team_eq(g.get('white_team'), cm['ta'])
        pa, pb = (g['white'], g['black']) if a_white else (g['black'], g['white'])
        ra, rb = (g.get('white_rating'), g.get('black_rating')) if a_white else (g.get('black_rating'), g.get('white_rating'))
        row.update(was=f"{row['a']} v {row['b']}", changed=True, a=pa, b=pb, a_white=a_white,
                   ra=ratings.get(L(pa)) or ra, rb=ratings.get(L(pb)) or rb, rank=None, chips=[],
                   alt_a=tip(pa, cm['ta']), alt_b=tip(pb, cm['tb']), game=g)

    free = [r for r in rows if not r['game']]
    for test in (lambda r, g: {L(r['a']), L(r['b'])} & {L(g['white']), L(g['black'])},
                 lambda r, g: r['a_white'] == team_eq(g.get('white_team'), cm['ta']),
                 lambda r, g: True):
        for g in list(rest):
            r = next((r for r in free if test(r, g)), None)
            if r:
                assign(r, g)
                free.remove(r)
                rest.remove(g)
    for g in rest:                                   # more games than Monday's boards (should not happen)
        extra = _rows_from_games([g], cm['ta'], ratings)[0]
        extra.update(changed=True, was=None, alt_a=tip(extra['a'], cm['ta']), alt_b=tip(extra['b'], cm['tb']))
        rows.append(extra)
    return rows


# =============================================================================
# PAGE PARTS
# =============================================================================

def _badges(card):
    if not card['badges']:
        return ''
    out = ''.join(f'<div class="bigb {BADGE_CLASS.get(b["name"], "b-top")}"><span class="bbn">{E(b["name"])}</span>'
                  f'<span class="bbr">{E(b["reason"])}</span></div>' for b in card['badges'])
    return f'<div class="bigbs">{out}</div>'


def _tape_head(card, side):
    team = card['t' + side]
    rows = card['season'][side]
    form = ''.join(f'<span class="ch {x["res"]}">{x["res"]}</span>' for x in rows)
    form = f'<span class="form" aria-label="Results so far">{form}</span>' if form else ''
    plc = card['p' + side]
    bits = ''
    if plc:
        bits += f'<span class="plc">{ordinal(plc)}</span>'
    if card.get('mp' + side) is not None and plc:
        bits += f'<span>{half(card["mp" + side])} match pts · {half(card["gp" + side])} game pts</span>'
    return (f'<div class="th th{side}"><div class="tname">{E(team)}</div>'
            f'<div class="tsub">{bits}{form}</div></div>')


def _live_strip(card):
    lv = card['live']
    if not lv['started']:
        return ''
    sc = f'<span class="lsa">{half(lv["sa"])}</span>–<span class="lsb">{half(lv["sb"])}</span>'
    if lv['left'] == 0:
        w = 'a' if lv['sa'] > lv['sb'] else 'b' if lv['sb'] > lv['sa'] else None
        tail = f'<b class="t{w}">Final · {E(card["t" + w])} win the match</b>' if w else '<b>Final · drawn match</b>'
        return f'<div class="live done"><div class="lsc">{sc}</div><div class="ltx">{tail}</div></div>'
    bits = [f'{plural(lv["left"], "board")} left']
    if lv['nxt']:
        bits.append(f'Next up: Bd {lv["nxt"][0]} · {fmt_time(lv["nxt"][1])}' if lv['nxt'][0] else
                    f'Next up: {fmt_time(lv["nxt"][1])}')
    dec = ''
    if lv['decided']:
        dec = f'<div class="ltx"><b class="dec t{lv["decided"]}">Decided: {E(card["t" + lv["decided"]])} wins</b></div>'
    return f'<div class="live"><div class="lsc">{sc}</div>{dec}<div class="ltx">{" · ".join(bits)}</div></div>'


def _odds(card):
    o = card.get('odds')
    if not o or card['live']['left'] == 0:
        return ''
    pa, pd, pb = o['probs']
    sa, sb = o['likely']
    note = ('<p class="pnote">Chances now, with the results so far</p>' if card['live']['started'] else '')
    return (f'<div class="oddsrow"><p class="oline"><span class="ta"><b>{E(card["ta"])}</b> win <b>{fmt_pct(pa)}</b></span> '
            f'<span class="sep">·</span> draw <b>{fmt_pct(pd)}</b> <span class="sep">·</span> '
            f'<span class="tb"><b>{E(card["tb"])}</b> win <b>{fmt_pct(pb)}</b></span> <span class="sep">·</span> '
            f'<span class="nw">most likely score <b>{half(sa)}-{half(sb)}</b></span></p>'
            f'<div class="bar"><div class="sa" style="width:{pa * 100:.1f}%"></div><div class="sd" style="width:{pd * 100:.1f}%">'
            f'</div><div class="sb" style="width:{pb * 100:.1f}%"></div></div>{note}</div>')


def _board_row(card, b, hl):
    g = b.get('game')
    st = b.get('state') or dict(state='unsched')
    tags = ''
    if b.get('changed'):
        tags = f'<span class="chg">changed since Monday{" (was " + E(b["was"]) + ")" if b.get("was") else ""}</span>'
    else:
        if b.get('rank') and b['rank'] <= 10:
            tags += f'<a class="rank" href="top_storylines.html">★ #{b["rank"]} storyline</a>'
        tags += ''.join(f'<span class="chip">{E(c)}</span>' for c in b.get('chips') or [])
    ca, cb = ('w', 'b') if b['a_white'] else ('b', 'w')
    col = lambda c: 'White' if c == 'w' else 'Black'
    wa = wb = ''
    bno = f'<span class="bno">Bd {b["board"]}</span>' if b.get('board') else ''
    status = ''
    if st['state'] == 'done':
        pa_, pb_ = (st['pw'], st['pb']) if b['a_white'] else (st['pb'], st['pw'])
        if pa_ > pb_:
            wa, wb = ' won', ' lost'
        elif pb_ > pa_:
            wa, wb = ' lost', ' won'
        res = f'{half(pa_)}–{half(pb_)}'
        if st.get('gid'):
            mid = f'{bno}<a class="res" href="https://lichess.org/{E(st["gid"])}" title="Game on Lichess">{res}</a>'
        else:
            mid = f'{bno}<span class="res un">{res}</span>'
        if st.get('kind'):
            status = f'<div class="when fk2">{E(st["kind"])}</div>'
    else:
        mid = f'{bno}<span class="vv">v</span>'
        if st['state'] == 'sched':
            status = f'<div class="when">🕒 {fmt_time(st["t"])}</div>'
        elif st['state'] == 'playing':
            started = f' · started {st["t"].strftime("%H:%M UTC")}' if st.get('t') else ''
            link = (f' · <a href="https://lichess.org/{E(st["gid"])}">watch</a>' if st.get('gid') else '')
            status = f'<div class="when now">Playing now{started}{link}</div>'
        elif st['state'] == 'late':
            status = f'<div class="when">Was scheduled {fmt_time(st["t"])} · no result yet</div>'
        elif card['live']['started'] or card['live']['has_calendar']:
            status = '<div class="when un">Not scheduled yet</div>'

    def rating(r):
        return f'<span class="rt">{E(r)}</span>' if r else ''
    nm_cls = lambda n: 'nm long' if len(n) > 16 else 'nm'
    line = (f'<div class="pa{wa}"><span class="{nm_cls(b["a"])}">{E(b["a"])}{alt_html(b.get("alt_a"))}</span><span class="rl">'
            f'<span class="dot {ca}" title="{col(ca)}"></span>{rating(b.get("ra"))}</span></div>'
            f'<div class="mid">{mid}</div>'
            f'<div class="pb{wb}"><span class="{nm_cls(b["b"])}">{E(b["b"])}{alt_html(b.get("alt_b"))}</span><span class="rl">'
            f'<span class="dot {cb}" title="{col(cb)}"></span>{rating(b.get("rb"))}</span></div>')
    cls = 'bd'
    if b.get('board') in hl and not b.get('changed'):
        cls += ' hl'
    if st['state'] == 'done':
        cls += ' fin' + (' ff' if st.get('kind') else '')
    chips = f'<div class="chips">{tags}</div>' if tags else ''
    return f'<div class="{cls}"><div class="bl">{line}</div>{status}{chips}</div>'


def _season_chart(card, nteams):
    ra, rb = card['season']['a'], card['season']['b']
    n = max(len(ra), len(rb))
    if n < 1 or not nteams:
        return ''
    W, H, pl, pr, pt, pb = 320, 116, 40, 44, 10, 22
    X = lambda i: pl + 10 + ((W - pl - pr - 10) * i / (n - 1) if n > 1 else (W - pl - pr - 10) / 2)
    Y = lambda v: pt + (H - pt - pb) * (v - 1) / max(nteams - 1, 1)
    s = (f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" class="ssc" role="img" '
         f'aria-label="Table place after each round, both teams">')
    for v in sorted({1, (nteams + 1) // 2, nteams}):
        s += (f'<line x1="{pl}" x2="{W - pr}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="gl"/>'
              f'<text x="{pl - 5}" y="{Y(v) + 4:.1f}" class="ax" text-anchor="end">{ordinal(v)}</text>')
    rounds = sorted({x['r'] for x in ra + rb})
    xi = {r: i for i, r in enumerate(rounds)}
    for r in rounds:
        s += f'<text x="{X(xi[r]):.1f}" y="{H - 4}" class="ax" text-anchor="middle">R{r}</text>'
    ends = []
    for rows, cls, dash in ((ra, 'la', ''), (rb, 'lb', ' stroke-dasharray="6 4"')):
        pts = [(X(xi[x['r']]), Y(x['pos'])) for x in rows if x.get('pos')]
        if not pts:
            continue
        s += f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}" class="{cls}"{dash}/>'
        for x, y in pts:
            s += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" class="{cls}c"/>'
        last = [x for x in rows if x.get('pos')][-1]
        ends.append([cls, last['pos'], Y(last['pos']) + 4, pts[-1][0]])
    ends.sort(key=lambda e: e[2])
    if ends:
        ends[0][2] = max(ends[0][2], 13)
    if len(ends) == 2:
        ends[1][2] = max(ends[1][2], ends[0][2] + 14)
    for cls, v, y, x in ends:
        s += f'<text x="{x + 8:.1f}" y="{y:.1f}" class="el {cls}t">{ordinal(v)}</text>'
    return s + '</svg>'


def chronicle_anchor(team1, team2):
    """Id of a match on Match Chronicles (matchups.html) - the same rule as team_matchup_report.chronicle_anchor:
    'm-<team>--<team>', names sorted, each slug cut at 30 characters. Change both together."""
    def slug(t):
        s = re.sub(r'[^a-z0-9]+', '-', ' '.join(str(t or '').split()).lower()).strip('-')
        return s[:30].strip('-') or 'team'
    x, y = sorted(' '.join(str(t or '').split()).lower() for t in (team1, team2))
    return f'm-{slug(x)}--{slug(y)}'


def _rounds_list(card, side, round_num=None):
    rows = card['season'][side]
    if not rows:
        return ''

    def chron(x):        # last round's match: its story on Match Chronicles (the site's matchups.html is that round)
        if round_num and x['r'] == round_num - 1 and x.get('opp'):
            return (f' · <a class="chr" href="matchups.html#{chronicle_anchor(card["t" + side], x["opp"])}">'
                    f'Chronicle →</a>')
        return ''
    lis = ''.join(f'<li><span class="rr">R{x["r"]}</span><span class="ch {x["res"]}">{x["res"]}</span>'
                  f'<span class="sc">{half(x["a"])}-{half(x["b"])}</span><span class="op">v {E(x["opp"])}{chron(x)}</span>'
                  f'<span class="aft" title="place after R{x["r"]}">{ordinal(x["pos"]) if x.get("pos") else ""}</span></li>'
                  for x in rows)
    return (f'<div class="blk"><div class="bh">Rounds so far <span class="k">result · opponent · place after</span></div>'
            f'<ul class="rounds">{lis}</ul></div>')


def _stake_lines(card, swing_min):
    st = card.get('stake')
    if not st:
        return ''
    lis = ''
    for side in 'ab':
        team = card['t' + side]
        info = st['teams'].get(team)
        if not info:
            continue
        key, lab = next(((k, lab) for k, lab in (('title', 'Title'), ('top3', 'Top 3'))
                         if info[k]['swing'] >= swing_min and info[k]['wdl'][0] is not None), (None, None))
        if not key:
            continue
        w, _, lo = info[key]['wdl']
        now = info[key].get('now')
        bits = (f"{fmt_pct(now)} now → <b>{fmt_pct(w)}</b> with a win" if now is not None else f"<b>{fmt_pct(w)}</b> with a win")
        if lo is not None:
            bits += f", {fmt_pct(lo)} with a loss"
        lis += (f'<li><span class="dot2 t{side}"></span><span><b class="t{side}">{E(team)}</b> · {lab} chance: '
                f'{bits}</span></li>')
    return f'<div class="stake"><div class="bh">At stake <span class="k">chance at the end of the season</span></div><ul>{lis}</ul></div>' if lis else ''


def _swing_badge(card, swing_min, featured=None):
    """'Biggest swing' reason from the featured match's stakes: the team / measure that moves most. featured = the
    round's start pick (prediction history): its numbers are those of the round's start, so the reason says 'Monday'."""
    st = featured or card['stake']
    best = None
    for key, lab in (('title', 'title'), ('top3', 'top-3')):
        for side in 'ab':
            info = (st['teams'].get(card['t' + side]) or {}).get(key)
            if info and info['swing'] >= swing_min and info['wdl'][0] is not None:
                if best is None or info['swing'] > best[0]:
                    best = (info['swing'], card['t' + side], lab, info['wdl'])
        if best:
            break
    if not best:
        return None
    _, team, lab, (w, _, lo) = best
    reason = f"{team}: {lab} chance {fmt_pct(lo)} with a loss, {fmt_pct(w)} with a win" if lo is not None else \
        f"{team}: {lab} chance {fmt_pct(w)} with a win"
    return {'name': 'Biggest swing', 'reason': ('Monday: ' if featured else '') + reason}


# =============================================================================
# CARDS
# =============================================================================

def _empty_card(ta, tb):
    return dict(ta=ta, tb=tb, pa=None, pb=None, mpa=None, mpb=None, gpa=None, gpb=None, boards=[], headline=None,
                also=None, badges=[], squad=None, who=None, season={'a': [], 'b': []}, monday=False)


def _refresh_headlines(cards):
    """Boards changed since Monday: a headline / Also fact resting on a changed board is dropped; the card takes its
    best remaining fact (not the neighbour cards' kind when there is a choice), Also = the next one of another kind
    (score 4+). Nothing left = no headline. Cards without changes keep Monday's choice."""
    def ok(f, changed):
        return f and not set(f.get('deps') if f.get('deps') is not None else f.get('boards') or ()) & changed

    for i, card in enumerate(cards):
        changed = card.get('changed_boards') or set()
        cands = card.get('candidates')
        if not changed or cands is None:
            continue
        valid = [f for f in cands if ok(f, changed)]
        if not valid:      # plain fallback: first meetings among the boards that kept Monday's pairing
            fm = sorted(b['board'] for b in card['boards'] if not b.get('changed') and b.get('met') is False and b.get('board'))
            if len(fm) >= 4:
                words = 'zero one two three four five six seven eight nine ten'.split()
                valid = [{'type': 'first_meet', 'text': f'First meetings on {words[len(fm)] if len(fm) <= 10 else len(fm)} boards',
                          'evidence': 'No earlier league game between the players on Bd ' + ', '.join(map(str, fm)),
                          'boards': [], 'deps': fm, 'score': 2}]
        head = card.get('headline')
        if not ok(head, changed):
            nbr = {(cards[j].get('headline') or {}).get('type') for j in (i - 1, i + 1) if 0 <= j < len(cards)}
            head = next((f for f in valid if f['type'] not in nbr or f['type'] in ('story1', 'milestone')),
                        valid[0] if valid else None)
            card['headline'] = head
        also = card.get('also')
        if head is None:
            card['also'] = None
        elif not ok(also, changed) or also['type'] == head['type']:
            card['also'] = next((f for f in valid if f['type'] != head['type'] and f.get('score', 0) >= 4
                                 and not (head['type'] == 'personal' and f['type'] == 'rematch')), None)


def _build_cards(round_games, predictions, centre, stakes, featured, calendar, places, standings, now, swing_min):
    ratings = {}
    for p in predictions:
        for bp in p['pairings']:
            for pl in (bp.white, bp.black):
                if not (pl.rating == 1500 and pl.rd == 350):        # 1500 / 350 = no rating found
                    ratings[pl.username.lower()] = pl.rating
    pred_by = {frozenset((p['team_a'], p['team_b'])): p for p in predictions}
    stake_by = {frozenset((m['a'], m['b'])): m for m in (stakes or [])}
    games_by = {}
    for g in round_games:
        if g.get('white') and g.get('black'):
            games_by.setdefault(frozenset((g.get('white_team'), g.get('black_team'))), []).append(g)

    def games_for(ta, tb):
        for k, gs in games_by.items():
            t1, t2 = (list(k) + list(k))[:2]
            if (team_eq(t1, ta) and team_eq(t2, tb)) or (team_eq(t1, tb) and team_eq(t2, ta)):
                return gs
        return []

    cards, seen = [], set()
    if centre:
        rosters = {t: set(v) for t, v in (centre.get('rosters') or {}).items()}
        for cm in centre['matches']:
            card = dict(_empty_card(cm['ta'], cm['tb']), **{k: cm.get(k) for k in (
                'pa', 'pb', 'mpa', 'mpb', 'gpa', 'gpb', 'headline', 'also', 'squad', 'who')})
            card['badges'] = list(cm.get('badges') or [])
            card['season'] = cm.get('season') or {'a': [], 'b': []}
            card['monday'] = True
            gs = games_for(cm['ta'], cm['tb'])
            card['boards'] = _match_boards(cm, gs, ratings, rosters, centre.get('alt_boards') or {})
            card['candidates'] = cm.get('candidates')
            changed = {b.get('board') for b in card['boards'] if b.get('changed')}
            card['changed_boards'] = changed
            if changed:            # a badge that rested on a changed board no longer holds: dropped, not passed on
                card['badges'] = [b for b in card['badges'] if not set(b.get('deps') or ()) & changed]
            cards.append(card)
            seen.add(frozenset(L(t) for t in (cm['ta'], cm['tb'])))
    _refresh_headlines(cards)
    rest = []
    for k, gs in games_by.items():
        t1, t2 = (list(k) + list(k))[:2]
        if frozenset((L(t1), L(t2))) in seen:
            continue
        # the left team: White on the first listed game (the old page's choice), stable for the same data
        p = pred_by.get(k)
        ta, tb = (p['team_a'], p['team_b']) if p else (gs[0]['white_team'], gs[0]['black_team'])
        card = _empty_card(ta, tb)
        if places:
            card['pa'], card['pb'] = places.get(ta), places.get(tb)
        if standings:
            for side, t in (('a', ta), ('b', tb)):
                s = standings.get(t) or {}
                card['mp' + side], card['gp' + side] = s.get('match_points'), s.get('game_points')
        rows = _rows_from_games(gs, ta, ratings)
        if calendar:                    # the calendar lists a match's games in board order
            order = {k2: i for i, k2 in enumerate(calendar)}
            rows.sort(key=lambda r: order.get(frozenset((L(r['a']), L(r['b']))), 999))
        card['boards'] = rows
        rest.append(card)
    rest.sort(key=lambda c: (min(c['pa'] or 999, c['pb'] or 999), max(c['pa'] or 999, c['pb'] or 999), c['ta'].lower()))
    cards += rest

    for card in cards:
        key = frozenset((card['ta'], card['tb']))
        p = next((v for kk, v in pred_by.items() if {L(t) for t in kk} == {L(t) for t in key}), None)
        if p:
            flip = not team_eq(p['team_a'], card['ta'])
            pa, pb = p['team_a_win_prob'], p['team_b_win_prob']
            dist = p.get('score_distribution') or {}
            exp_a = p['team_a_expected']
            if dist:
                likely = max(dist.items(), key=lambda kv: (kv[1], -abs(kv[0][0] - exp_a)))[0]
            else:
                likely = (round(exp_a * 2) / 2, round(p['team_b_expected'] * 2) / 2)
            if flip:
                pa, pb = pb, pa
                likely = likely[::-1]
            card['odds'] = {'probs': (pa, p['draw_prob'], pb), 'likely': likely}
        card['stake'] = next((v for kk, v in stake_by.items() if {L(t) for t in kk} == {L(t) for t in key}), None)
        # live state
        sa = sb = 0.0
        left, started, nxt = 0, False, None
        for b in card['boards']:
            g = b.get('game')
            b['state'] = _board_state(g, calendar, now) if g else dict(state='unsched')
            st = b['state']
            if st['state'] == 'done':
                pa_, pb_ = (st['pw'], st['pb']) if b['a_white'] else (st['pb'], st['pw'])
                sa += pa_
                sb += pb_
                started = True
            else:
                left += 1
                if st['state'] == 'playing':
                    started = True
                if st['state'] == 'sched' and (nxt is None or st['t'] < nxt[1]):
                    nxt = (b.get('board'), st['t'])
        card['live'] = dict(sa=sa, sb=sb, left=left, nxt=nxt, started=started, has_calendar=bool(calendar),
                            decided='a' if sa - sb > left else 'b' if sb - sa > left else None)
        if featured and {card['ta'], card['tb']} == {featured['a'], featured['b']}:
            card['featured'] = True
            sw = _swing_badge(card, swing_min, featured if card['stake'] is not featured else None)
            if sw:
                card['badges'] = [sw] + card['badges'][:1]          # still max 2 badges
    return cards


# =============================================================================
# PAGE
# =============================================================================

CSS = '''
:root { --bg:#f0f6f6; --card:#fff; --card-2:#e7f1f2; --ink:#0f2830; --muted:#5a7781; --teal:#2c5f6f; --deep:#1a4a5c;
  --pale:#a8d5e2; --gold:#d4b856; --line:#d3e3e6; --team-a:#2c5f6f; --team-b:#9a4f2a; --tint-a:#e4eff2; --tint-b:#f6ebe4; --draw:#d4b856; --mine:#fbf3d6; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:var(--bg); color:var(--ink); line-height:1.4; font-size:15px; }
.topbar { background:var(--deep); padding:0.6rem 16px; display:flex; flex-wrap:wrap; align-items:center; gap:0.4rem 1.25rem; }
.topbar a { color:#fff; text-decoration:none; } .topbar .back { font-weight:600; white-space:nowrap; }
.pages { display:flex; flex-wrap:wrap; gap:0.35rem; }
.pages a { font-size:0.85rem; padding:0.2rem 0.65rem; border-radius:999px; border:1px solid rgba(255,255,255,0.35); white-space:nowrap; }
.pages a.on { background:#fff; color:var(--deep); font-weight:600; border-color:#fff; }
.page { max-width:1100px; margin:0 auto; padding:1.25rem 16px 3rem; }
h1 { text-align:center; color:var(--teal); font-size:1.8rem; line-height:1.2; margin-bottom:0.3rem; }
.subtitle { text-align:center; color:var(--muted); margin-bottom:0.4rem; }
.updated { text-align:center; font-size:0.85rem; color:var(--deep); font-weight:600; margin:0 auto 0.7rem; }
.intro { text-align:center; font-size:0.9rem; margin:0 auto 0.8rem; max-width:620px; }
.picker { display:flex; flex-wrap:wrap; align-items:center; justify-content:center; gap:0.5rem; margin:0 auto 0.8rem; }
.picker label { color:var(--muted); font-size:0.9rem; }
.picker select { font:inherit; font-size:0.9rem; max-width:100%; padding:0.35rem 0.5rem; border:1px solid var(--pale); border-radius:8px; background:var(--card); color:var(--ink); }
.picker button, .unfold { font:inherit; font-size:0.85rem; padding:0.3rem 0.8rem; border:1px solid var(--teal); border-radius:8px; background:var(--card); color:var(--teal); cursor:pointer; }
.picker button[hidden] { display:none; }
.key { display:flex; flex-wrap:wrap; justify-content:center; gap:0.3rem 0.9rem; font-size:0.8rem; color:var(--muted); margin:0 auto 1.3rem; max-width:760px; }
.key > span { display:inline-flex; align-items:center; gap:0.3rem; }
.key .kt { display:block; text-align:center; flex-basis:100%; } .rk { color:#7a5d10; }
.k { color:var(--muted); font-weight:400; } .nw { white-space:nowrap; }
.ta { color:var(--team-a); } .tb { color:var(--team-b); }
.list { display:flex; flex-direction:column; gap:1.5rem; }
.mc { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; box-shadow:0 1px 3px rgba(15,40,48,0.08); scroll-margin-top:8px; }
.mc.mine { border:2px solid var(--gold); box-shadow:0 0 0 3px rgba(212,184,86,0.35); } .mc.mine .hd { background:var(--mine); }
.mine-tag { font-size:0.75rem; font-weight:700; padding:0.05rem 0.5rem; border-radius:999px; background:var(--gold); color:var(--ink); white-space:nowrap; }
.hd { padding:0.8rem 1rem 0.7rem; }
.top { display:flex; flex-wrap:wrap; align-items:center; gap:0.3rem 0.5rem; width:100%; }
.meta { font-size:0.8rem; color:var(--muted); font-weight:600; }
.up { margin-left:auto; font-size:0.8rem; color:var(--teal); text-decoration:none; white-space:nowrap; font-weight:600; }
.headline { font-size:1.3rem; color:var(--deep); margin:0.15rem 0; line-height:1.25; overflow-wrap:break-word; }
.hdet { font-size:0.9rem; margin-bottom:0.2rem; overflow-wrap:break-word; }
.fact { font-size:0.9rem; margin-top:0.3rem; overflow-wrap:break-word; }
.fk { font-size:0.75rem; font-weight:700; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em; margin-right:0.3rem; }
.pn { font-weight:600; white-space:nowrap; }
.nm, .nmx { white-space:nowrap; }   /* player names never wrap (not even at a hyphen) */
.bigbs { display:flex; flex-wrap:wrap; gap:0.4rem; margin:0 0 0.5rem; }
.bigb { display:inline-flex; align-items:stretch; border-radius:8px; overflow:hidden; font-size:0.85rem; max-width:100%; }
.bbn { padding:0.25rem 0.6rem; font-weight:800; text-transform:uppercase; letter-spacing:0.06em; color:#fff; white-space:nowrap; display:flex; align-items:center; }
.bbr { padding:0.25rem 0.6rem; background:#fff; font-weight:600; display:flex; align-items:center; overflow-wrap:break-word; min-width:0; }
.b-swing .bbn { background:#1a4a5c; } .b-swing { border:2px solid #1a4a5c; }
.b-top .bbn { background:#24657a; } .b-top { border:2px solid #24657a; }
.b-conn .bbn { background:#d4b856; color:#0f2830; } .b-conn { border:2px solid #d4b856; }
.b-rem .bbn { background:#3d7a8a; } .b-rem { border:2px solid #3d7a8a; }
.b-champ .bbn { background:#8a6d1c; } .b-champ { border:2px solid #8a6d1c; }
.b-mil .bbn { background:#5f7c86; } .b-mil { border:2px solid #5f7c86; }
.b-debut .bbn { background:#2f7e9b; } .b-debut { border:2px solid #2f7e9b; }
.b-close .bbn { background:#2c5f6f; } .b-close { border:2px solid #2c5f6f; }
.tape { display:grid; grid-template-columns:minmax(0,1fr) 2.6rem minmax(0,1fr); }
.th { padding:0.85rem 1rem 0.8rem; color:#fff; display:flex; flex-direction:column; justify-content:space-between; gap:0.5rem; min-width:0; }
.tha { background:var(--team-a); } .thb { background:var(--team-b); text-align:right; }
.tname { font-size:1.2rem; font-weight:800; line-height:1.2; overflow-wrap:break-word; }
.tsub { display:flex; flex-wrap:wrap; align-items:center; gap:0.3rem 0.6rem; font-size:0.85rem; }
.thb .tsub { justify-content:flex-end; }
.plc { font-size:1.05rem; font-weight:800; background:rgba(255,255,255,0.18); border-radius:6px; padding:0 0.4rem; }
.form { display:inline-flex; gap:2px; }
.vs { display:flex; align-items:center; justify-content:center; background:linear-gradient(90deg,var(--team-a) 50%,var(--team-b) 50%); }
.vs span { width:2.1rem; height:2.1rem; border-radius:50%; background:#fff; color:var(--deep); font-weight:800; display:flex; align-items:center; justify-content:center; font-size:0.95rem; }
.live { display:flex; flex-direction:column; align-items:center; gap:0.1rem; padding:0.5rem 1rem 0.55rem; background:#fff; border-bottom:1px solid var(--line); text-align:center; }
.lsc { font-size:1.6rem; font-weight:800; letter-spacing:0.02em; line-height:1.2; } .lsa { color:var(--team-a); } .lsb { color:var(--team-b); }
.ltx { font-size:0.9rem; color:var(--ink); overflow-wrap:break-word; }
.live .dec { background:#f6ecc6; border-radius:6px; padding:0 0.4rem; }
.oddsrow { padding:0.6rem 1rem 0.7rem; background:var(--card-2); border-bottom:1px solid var(--line); }
.oline { font-size:0.9rem; margin-bottom:0.3rem; overflow-wrap:break-word; } .oline .sep { color:#8fb3bd; }
.bar { display:flex; height:12px; border-radius:6px; overflow:hidden; }
.sa { background:var(--team-a); } .sd { background:var(--draw); } .sb { background:var(--team-b); }
.pnote { font-size:0.8rem; color:var(--muted); margin-top:0.3rem; }
.sec { padding:0.9rem 1rem 0.2rem; }
.sec:last-child { padding-bottom:1rem; }
.bh { font-size:0.75rem; color:var(--muted); font-weight:700; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:0.4rem; display:flex; align-items:baseline; gap:0.4rem; flex-wrap:wrap; }
.bh .k { text-transform:none; letter-spacing:0; font-weight:400; font-size:0.8rem; }
.boards { display:flex; flex-direction:column; gap:4px; }
.bd { background:var(--card-2); border-radius:8px; padding:0.35rem 0.5rem; border-left:4px solid transparent; }
.bd.hl { border-left-color:var(--gold); background:#f3f1e3; }
.bd.fin { background:#eef5f2; } .bd.hl.fin { background:#f1f0e2; }
.bl { display:grid; grid-template-columns:minmax(0,1fr) 3.2rem minmax(0,1fr); gap:0 0.5rem; align-items:center; font-size:0.9rem; }
.pa, .pb { display:flex; flex-wrap:wrap; align-items:center; gap:0.1rem 0.45rem; min-width:0; }
.pa { justify-content:flex-end; text-align:right; } .pb { flex-direction:row-reverse; justify-content:flex-end; }
.pb .rl { flex-direction:row-reverse; }
.nm { font-weight:700; overflow-wrap:break-word; line-height:1.25; min-width:0; }
.pa .nm { color:var(--team-a); } .pb .nm { color:var(--team-b); }
.pa.won .nm, .pb.won .nm { font-weight:800; }
.pa.lost .nm, .pb.lost .nm { font-weight:500; opacity:0.75; }
.bd.ff .nm { font-weight:500; opacity:0.7; }
.rl { display:inline-flex; align-items:center; gap:0.3rem; }
.rt { color:var(--muted); font-size:0.8rem; }
.mid { display:flex; flex-direction:row; gap:0.35rem; align-items:center; justify-content:center; line-height:1.1; }
.bno { font-size:0.75rem; font-weight:700; color:var(--muted); white-space:nowrap; }
.vv { font-weight:800; color:var(--deep); }
.res { font-weight:800; color:var(--ink); text-decoration:none; border-bottom:1px dotted var(--muted); white-space:nowrap; }
.res.un { color:var(--muted); border-bottom:none; }
.when { text-align:center; font-size:0.8rem; color:var(--muted); margin-top:0.15rem; }
.when.now { color:#2f6e48; font-weight:700; } .when.now a { color:inherit; }
.when.un { font-style:italic; } .when.fk2 { font-style:italic; }
.chg { font-size:0.75rem; font-weight:700; background:#fbe3d6; color:#7a3415; border-radius:999px; padding:0.05rem 0.55rem; overflow-wrap:break-word; }
.dot { width:10px; height:10px; border-radius:50%; flex:none; display:inline-block; } .dot.w { background:#fff; border:1.5px solid #7a8f95; } .dot.b { background:#2a3b40; }
.chips { display:flex; flex-wrap:wrap; justify-content:center; gap:0.25rem; margin-top:0.3rem; }
.chip { font-size:0.75rem; background:var(--card); border:1px solid var(--line); border-radius:999px; padding:0.05rem 0.5rem; overflow-wrap:break-word; }
.rank { font-size:0.75rem; font-weight:700; color:#7a5d10; background:#f6ecc6; border-radius:999px; padding:0.05rem 0.5rem; text-decoration:none; white-space:nowrap; }
.squad { background:#e3f0f3; border:1px solid #bcd9e0; border-left:4px solid var(--teal); border-radius:8px; padding:0.6rem 0.8rem; font-size:0.85rem; }
.squad .bh { color:var(--deep); }
.sline { display:flex; gap:0.45rem; align-items:baseline; }
.si { flex:none; width:1.3rem; text-align:center; font-size:0.95rem; }
.squad ul { list-style:none; display:flex; flex-direction:column; gap:0.25rem; margin-top:0.15rem; }
.squad li { overflow-wrap:break-word; } .squad .sl { margin-left:1.75rem; list-style:disc; }
.lead { font-size:0.9rem; } .lead2 { margin-top:0.15rem; }
.tmh { font-weight:700; font-size:0.85rem; margin-top:0.45rem; padding-top:0.4rem; border-top:1px dashed #bcd9e0; }
.old { font-weight:600; }
.chartwrap { display:flex; flex-wrap:wrap; align-items:center; gap:0.3rem 1.2rem; margin-bottom:0.6rem; }
.ssc { display:block; max-width:100%; height:auto; flex:none; }
.ssc .gl { stroke:#d3e3e6; stroke-width:1; } .ssc .ax { fill:var(--muted); font-size:12px; }
svg .la { stroke:var(--team-a); fill:none; stroke-width:2.5; stroke-linejoin:round; } svg .lb { stroke:var(--team-b); fill:none; stroke-width:2.5; stroke-linejoin:round; }
.ssc .lac { fill:var(--team-a); } .ssc .lbc { fill:#fff; stroke:var(--team-b); stroke-width:2; }
.ssc .el { font-size:13px; font-weight:800; } .ssc .lat { fill:var(--team-a); } .ssc .lbt { fill:var(--team-b); }
.ckey { display:flex; flex-direction:column; gap:0.3rem; font-size:0.85rem; font-weight:600; min-width:0; flex:1 1 200px; }
.ckey span { display:flex; gap:0.4rem; align-items:baseline; overflow-wrap:break-word; } .ckey svg { flex:none; }
.ckey span:first-child { color:var(--team-a); } .ckey span:last-child { color:var(--team-b); }
.cols { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:0.8rem; }
.col { border-radius:8px; padding:0.6rem 0.75rem 0.75rem; display:flex; flex-direction:column; gap:0.75rem; min-width:0; }
.cola { background:var(--tint-a); border-top:4px solid var(--team-a); } .colb { background:var(--tint-b); border-top:4px solid var(--team-b); }
.colh { font-weight:800; font-size:0.95rem; line-height:1.25; overflow-wrap:break-word; }
.cola .colh, .cola .bh { color:var(--team-a); } .colb .colh, .colb .bh { color:var(--team-b); }
.rounds { list-style:none; display:flex; flex-direction:column; gap:3px; font-size:0.85rem; }
.rounds li { display:grid; grid-template-columns:1.6rem 1.3rem 3.3rem minmax(0,1fr) auto; gap:0.4rem; align-items:baseline; background:rgba(255,255,255,0.8); border-radius:5px; padding:0.2rem 0.4rem; }
.rr { color:var(--muted); font-size:0.8rem; }
.ch { font-size:0.75rem; font-weight:800; width:1.3rem; text-align:center; border-radius:3px; color:#fff; display:inline-block; line-height:1.35; }
.ch.W { background:#3c8a5a; } .ch.D { background:var(--draw); color:var(--ink); } .ch.L { background:#b05a4a; }
.th .ch { box-shadow:0 0 0 1px rgba(255,255,255,0.6); }
.sc { font-weight:700; } .op { overflow-wrap:break-word; } .chr { color:var(--teal); font-weight:700; white-space:nowrap; } .aft { color:var(--muted); font-size:0.8rem; white-space:nowrap; }
.ppl { display:flex; flex-direction:column; gap:0.45rem; font-size:0.85rem; }
.ppk { font-weight:700; display:flex; align-items:center; gap:0.35rem; } .ppi { width:2.1rem; flex:none; text-align:center; }
.pp ul { list-style:none; display:flex; flex-wrap:wrap; gap:0.1rem 0.9rem; padding-left:2.45rem; }
.nmx { font-weight:600; overflow-wrap:break-word; }
.unfold { display:none; margin-top:0.4rem; }
.stake { margin-top:0.8rem; background:var(--card-2); border-radius:8px; padding:0.5rem 0.75rem; font-size:0.9rem; }
.stake ul { list-style:none; display:flex; flex-direction:column; gap:0.25rem; }
.stake li { display:flex; gap:0.45rem; align-items:baseline; overflow-wrap:break-word; }
.dot2 { width:9px; height:9px; border-radius:50%; flex:none; display:inline-block; } .dot2.ta { background:var(--team-a); } .dot2.tb { background:var(--team-b); }
.foot { color:var(--muted); font-size:0.8rem; margin-top:1.5rem; text-align:center; }
.altb { position:relative; display:inline-block; font-size:0.75rem; font-weight:700; letter-spacing:0.04em; color:var(--muted); box-shadow:inset 0 0 0 1px #9fb7be; border-radius:3px; padding:0 0.3rem; margin:0 0.35rem; vertical-align:2px; line-height:1.3; cursor:help; }
.altb:focus { outline:2px solid var(--pale); }
.altb:focus::after { content:attr(data-tip); position:absolute; top:calc(100% + 4px); left:50%; transform:translateX(-50%); width:max-content; max-width:220px; white-space:normal; background:var(--deep); color:#fff; font-weight:600; letter-spacing:0; padding:0.3rem 0.5rem; border-radius:6px; z-index:5; text-align:left; font-size:0.8rem; }
.pa .altb:focus::after { left:auto; right:0; transform:none; } .pb .altb:focus::after { left:0; transform:none; }
.ml { background:var(--card); border:1px solid var(--line); border-radius:12px; margin:0 auto 1.3rem; }
.ml summary { cursor:pointer; font-weight:700; color:var(--deep); padding:0.6rem 1rem; list-style:none; display:flex; justify-content:space-between; align-items:center; }
.ml summary::-webkit-details-marker { display:none; }
.ml summary::after { content:'▾'; color:var(--muted); } .ml[open] summary::after { content:'▴'; }
.mll { list-style:none; border-top:1px solid var(--line); }
.mll li + li { border-top:1px solid #e6eff1; }
.mlr { display:grid; grid-template-columns:1.8rem minmax(0,1fr); gap:0.5rem; padding:0.4rem 1rem; text-decoration:none; color:var(--ink); }
.mlr:hover { background:var(--card-2); }
.mlr.mine { background:var(--mine); box-shadow:inset 4px 0 0 var(--gold); }
.mln { font-weight:800; color:var(--muted); font-size:0.85rem; text-align:right; padding-top:0.05rem; }
.mlm { display:grid; grid-template-columns:minmax(0,1.3fr) minmax(0,1fr); gap:0.1rem 1rem; min-width:0; }
.mlt { font-weight:700; font-size:0.85rem; overflow-wrap:break-word; line-height:1.3; }
.mlh { font-size:0.85rem; color:var(--deep); overflow-wrap:break-word; line-height:1.3; }
.mls { font-weight:800; white-space:nowrap; }
.mlb { display:inline-block; font-size:0.75rem; font-weight:800; text-transform:uppercase; letter-spacing:0.04em; color:#fff; border-radius:4px; padding:0 0.35rem; margin-right:0.35rem; }
.mlb.b-swing { background:#1a4a5c; } .mlb.b-top { background:#24657a; } .mlb.b-conn { background:#d4b856; color:#0f2830; } .mlb.b-rem { background:#3d7a8a; }
.mlb.b-champ { background:#8a6d1c; } .mlb.b-mil { background:#5f7c86; } .mlb.b-debut { background:#2f7e9b; } .mlb.b-close { background:#2c5f6f; }
@media (max-width:720px) {
  h1 { font-size:1.4rem; }
  .hd { padding:0.7rem 0.8rem 0.6rem; }
  .headline { font-size:1.15rem; }
  .bigb { flex-direction:column; } .bbn { padding:0.2rem 0.6rem; }
  .tape { grid-template-columns:minmax(0,1fr); }
  .th { padding:0.7rem 0.8rem; } .thb { text-align:left; } .thb .tsub { justify-content:flex-start; }
  .vs { background:linear-gradient(var(--team-a) 50%, var(--team-b) 50%); padding:0.3rem 0; }
  .oddsrow { padding:0.55rem 0.8rem 0.65rem; }
  .sec { padding:0.8rem 0.6rem 0.2rem; }
  .bl { grid-template-columns:2.7rem minmax(0,1fr); gap:0.1rem 0.4rem; font-size:0.9rem; align-items:baseline; }
  .mid { display:contents; }
  .bno { grid-row:1; grid-column:1; }
  .mid > .vv, .mid > .res { grid-row:2; grid-column:1; justify-self:start; }
  .pa { grid-row:1; grid-column:2; justify-content:flex-start; text-align:left; }
  .pb { grid-row:2; grid-column:2; flex-direction:row; justify-content:flex-start; }
  .pb .rl { flex-direction:row; }
  .nm.long { font-size:0.9em; }
  .when, .chips { text-align:left; justify-content:flex-start; margin-left:3.1rem; }
  .cols { grid-template-columns:minmax(0,1fr); }
  .ppl.fold:not(.open) .pp.hide { display:none; }
  .ppl.fold:not(.open) li.hide { display:none; }
  .ppl.fold + .unfold { display:inline-block; align-self:flex-start; }
  .ppl.fold.open + .unfold { display:none; }
  .mlm { grid-template-columns:minmax(0,1fr); }
  .mlr { padding:0.4rem 0.75rem; }
}
'''

JS = '''
(function () {
  var KEY = '4545-predictions-my-team';
  var sel = document.getElementById('team-pick'), clear = document.getElementById('team-clear'), list = document.getElementById('match-list');
  if (!sel || !list) return;
  function apply(team) {
    var items = Array.prototype.slice.call(list.children).filter(function (e) { return e.hasAttribute('data-i'); });
    items.sort(function (a, b) { return a.getAttribute('data-i') - b.getAttribute('data-i'); });
    items.forEach(function (e) { e.classList.remove('mine'); var t = e.querySelector('.mine-tag'); if (t) t.parentNode.removeChild(t); list.appendChild(e); });
    items.forEach(function (e) {
      if (team && (e.getAttribute('data-ta') === team || e.getAttribute('data-tb') === team)) {
        e.classList.add('mine');
        var tag = document.createElement('span'); tag.className = 'mine-tag'; tag.textContent = 'Your match';
        var top = e.querySelector('.top'); top.insertBefore(tag, top.firstChild);
        list.insertBefore(e, list.firstChild);
      }
    });
    Array.prototype.forEach.call(document.querySelectorAll('.mlr'), function (r) {
      r.classList.toggle('mine', !!team && (r.getAttribute('data-ta') === team || r.getAttribute('data-tb') === team));
    });
    clear.hidden = !team;
  }
  function remember(t) { try { if (t) { localStorage.setItem(KEY, t); } else { localStorage.removeItem(KEY); } } catch (e) {} }
  sel.addEventListener('change', function () { remember(sel.value); apply(sel.value); });
  clear.addEventListener('click', function () { sel.value = ''; remember(''); apply(''); });
  var saved = ''; try { saved = localStorage.getItem(KEY) || ''; } catch (e) {}
  if (saved && Array.prototype.some.call(sel.options, function (o) { return o.value === saved; })) { sel.value = saved; apply(saved); }
  Array.prototype.forEach.call(document.querySelectorAll('.unfold'), function (b) {
    b.addEventListener('click', function () { b.previousElementSibling.classList.add('open'); });
  });
})();
'''


def _card_html(i, n, card, nteams, swing_min, round_num=None):
    hl = set((card.get('headline') or {}).get('boards') or [])
    head = ''
    if card.get('headline'):
        h = card['headline']
        head = f'<h2 class="headline">{E(h["text"])}</h2>' + (f'<p class="hdet">{h["evidence"]}</p>' if h.get('evidence') else '')
    if card.get('also'):
        x = card['also']
        head += f'<p class="fact"><span class="fk">Also</span> <b>{E(x["text"])}</b>{" · " + x["evidence"] if x.get("evidence") else ""}</p>'
    where = f' · {ordinal(card["pa"])} v {ordinal(card["pb"])} in the table' if card.get('pa') and card.get('pb') else ''
    anchor = '<span id="stakes"></span>' if card.get('featured') else ''
    sections = (f'<section class="sec"><div class="bh">The boards <span class="k">left team’s player on the left · '
                f'○ White ● Black</span></div><div class="boards">'
                f'{"".join(_board_row(card, b, hl) for b in card["boards"])}</div></section>')
    changed = any(b.get('changed') for b in card['boards'])
    monday_note = ' <span class="k">Monday’s line-up</span>' if changed else ''
    if card.get('squad'):
        sections += (f'<section class="sec"><div class="squad"><div class="bh">Squad links{monday_note}</div>'
                     f'{card["squad"]}</div></section>')
    stakes = _stake_lines(card, swing_min)
    chart = _season_chart(card, nteams)
    if chart or card.get('who') or stakes:
        inner = ''
        if chart:
            inner += (f'<div class="bh">Season so far <span class="k">table place after each round</span></div>'
                      f'<div class="chartwrap">{chart}<div class="ckey"><span><svg width="24" height="8" aria-hidden="true">'
                      f'<line x1="1" x2="23" y1="4" y2="4" class="la"/></svg>{E(card["ta"])}</span><span><svg width="24" '
                      f'height="8" aria-hidden="true"><line x1="1" x2="23" y1="4" y2="4" class="lb" stroke-dasharray="6 4"/>'
                      f'</svg>{E(card["tb"])}</span></div></div>')
        if chart or card.get('who'):
            cols = ''
            for side in 'ab':
                who = (card.get('who') or {}).get(side, '')
                if changed:
                    who = who.replace('<div class="bh">Who’s on the boards</div>',
                                      f'<div class="bh">Who’s on the boards{monday_note}</div>')
                cols += (f'<div class="col col{side}"><div class="colh">{E(card["t" + side])}</div>'
                         f'{_rounds_list(card, side, round_num)}{who}</div>')
            inner += f'<div class="cols">{cols}</div>'
        inner += stakes
        sections += f'<section class="sec teams">{inner}</section>'
    return f'''<article class="mc" id="m{i + 1}" data-i="{i}" data-ta="{E(card["ta"])}" data-tb="{E(card["tb"])}">
<header class="hd">{anchor}
  {_badges(card)}<div class="top"><span class="meta">Match {i + 1} of {n}{where}</span><a class="up" href="#mlist">↑ All matches</a></div>
  {head}
</header>
<div class="tape">{_tape_head(card, 'a')}<div class="vs"><span>v</span></div>{_tape_head(card, 'b')}</div>
{_live_strip(card)}{_odds(card)}
{sections}
</article>'''


def _list_row(i, card):
    tags = ''.join(f'<span class="mlb {BADGE_CLASS.get(b["name"], "b-top")}">{E(b["name"])}</span>' for b in card['badges'])
    lv = card['live']
    text = E(card['headline']['text']) if card.get('headline') else ''
    if lv['started']:
        score = (f'<span class="mls"><span class="ta">{half(lv["sa"])}</span>–<span class="tb">{half(lv["sb"])}</span>'
                 f'{"" if lv["left"] else " final"}</span> ')
        text = score + text
    return (f'<li><a class="mlr" href="#m{i + 1}" data-ta="{E(card["ta"])}" data-tb="{E(card["tb"])}"><span class="mln">{i + 1}</span>'
            f'<span class="mlm"><span class="mlt"><span class="ta">{E(card["ta"])}</span> <span class="k">v</span> '
            f'<span class="tb">{E(card["tb"])}</span></span><span class="mlh">{tags}{text}</span></span></a></li>')


def render(season, round_num, round_games, predictions, centre=None, stakes=None, featured=None, calendar=None,
           places=None, standings=None, nav=None, now=None, n_sims=None, swing_min=0.10):
    """The whole page. round_games: the season's games of this round (as the API gives them); predictions:
    match_predictor.simulate_match results; centre: load_data(); stakes / featured: match_stakes() /
    featured_stake() of a --full-season run (else None); calendar: parse_calendar() or None; places / standings:
    team -> place / {'match_points', 'game_points'} before the round (used when there is no JSON);
    nav: [(label, href, is_current)]."""
    now = now or datetime.now(timezone.utc)
    cards = _build_cards(round_games, predictions, centre, stakes, featured, calendar, places, standings, now, swing_min)
    nteams = (centre or {}).get('nteams') or (len(places) if places else 0)
    n = len(cards)
    any_started = any(c['live']['started'] for c in cards)
    all_done = n and all(c['live']['left'] == 0 for c in cards)
    state = 'round finished' if all_done else 'during the round' if any_started else 'before the round'
    any_result = any(b.get('state', {}).get('state') == 'done' for c in cards for b in c['boards'])
    forfeit_key = ' Forfeits count in the score and are shown in grey.' if any_result else ''
    teams = sorted({t for c in cards for t in (c['ta'], c['tb'])}, key=str.lower)
    opts = ''.join(f'<option value="{E(t)}">{E(t)}</option>' for t in teams)
    links = ''.join(f'<a class="on" aria-current="page" href="{E(h)}">{E(lab)}</a>' if on else f'<a href="{E(h)}">{E(lab)}</a>'
                    for lab, h, on in (nav or [('Match Centre', 'match_centre.html', True)]))
    rows = ''.join(_list_row(i, c) for i, c in enumerate(cards))
    body = ''.join(_card_html(i, n, c, nteams, swing_min, round_num) for i, c in enumerate(cards))
    updated = now.strftime('%a %d %b, %H:%M UTC')
    sims = f'{n_sims:,}' if n_sims else 'many'
    monday = bool(centre)
    key_extra = (' Under each board: its storylines; <b class="rk">★ #N</b> = place among the round’s top storylines. '
                 'A gold edge marks the boards the headline is about.' if monday else '')
    alt_key = '<span class="altb">alt</span> = alternate (not on the team’s roster; hover or tap).' if monday else ''
    foot_monday = ('<br>Earlier games = played league games only (no forfeits). Milestones as in Top Storylines (45th, 100th, '
                   '200th, 300th game). Centurions = 100+ league games. Champions and medallists = on the team’s final roster. '
                   'Former teammates: same team in the same season, both 4+ games for it or both on its final roster. '
                   'Most connected = earlier league games + former-teammate pairs + LoneWolf / 960 games between the two teams.'
                   if monday else '')
    boards_note = ('Places and points before the round; board order from the league calendar and the team rosters. '
                   if monday else 'Board order not known yet: boards in calendar or rating order. ')
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Match Centre - Round {round_num} - Season {season}</title>
<style>{CSS}</style>
</head>
<body>
<nav class="topbar">
  <a class="back" href="index.html">← The 4545 Chronicles</a>
  <div class="pages">{links}</div>
</nav>
<main class="page">
  <h1>Match Centre · Round {round_num}</h1>
  <p class="subtitle">Season {season} · {plural(n, 'team match', 'team matches')} · {state}</p>
  <p class="updated">Updated {updated} · results, scheduled times and chances refresh 4 times a day</p>
  <p class="intro">Every match of the round in table order. On each card the left team is teal and the right team rust, on every board and chart.</p>
  <div class="picker"><label for="team-pick">Your team:</label>
    <select id="team-pick"><option value="">Choose a team…</option>{opts}</select>
    <button type="button" id="team-clear" hidden>Clear</button></div>
  <div class="key">
    <span><span class="dot w"></span>White</span><span><span class="dot b"></span>Black</span>
    <span class="kt">{alt_key}{key_extra}{forfeit_key}</span></div>
  <details class="ml" id="mlist"><summary>All {n} matches</summary><ol class="mll">{rows}</ol></details>
  <div class="list" id="match-list">
{body}
  </div>
  <p class="foot">{boards_note}Chances: {sims} simulations of each match from the players’ Lichess classical ratings
  (most likely score = the most common final score in them). At stake and Biggest swing: chance at the end of the season after
  a win or a loss in this match, from the simulations of the whole season.{foot_monday}</p>
</main>
<script>{JS}</script>
</body>
</html>
'''
