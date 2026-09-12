#!/usr/bin/env python3
"""
CueScore Live Match Tracker
Monitoruje turniej bilardowy i wysyła powiadomienia macOS gdy zmienia się wynik śledzionego zespołu.
"""

import json
import time
import urllib.request
import subprocess
import re
import sys
import os
import threading
from datetime import datetime

# ─── KONFIGURACJA ──────────────────────────────────────────────────────────────
DEFAULT_TOURNAMENT_URL = "https://cuescore.com/tournament/MIX+CUP+-+SCOTCH+DOUBLES+-+Baltic+Billiard+Festival/82140400"
POLL_INTERVAL = 15  # sekundy między odpytaniami API
STATE_FILE = os.path.join(os.path.dirname(__file__), ".tracker_state.json")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "tracker_config.json")
# ───────────────────────────────────────────────────────────────────────────────


def extract_tournament_id(url: str) -> str:
    """Wyciąga ID turnieju z URL CueScore."""
    match = re.search(r"/(\d{6,})/?$", url.strip())
    return match.group(1) if match else None


def fetch_tournament(tournament_id: str) -> dict:
    """Pobiera dane turnieju z API CueScore."""
    api_url = f"https://api.cuescore.com/tournament/?id={tournament_id}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "BilliardTracker/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[błąd API] {e}")
        return None


NTFY_URL = "https://ntfy.sh/bilard-tracker-wiktoria"


ON_MAC = sys.platform == "darwin"


def notify(title: str, message: str, sound: bool = True):
    """Wysyła powiadomienie macOS (jeśli działa lokalnie) i na iPhone przez ntfy.sh."""
    if ON_MAC:
        sound_part = 'sound name "Glass"' if sound else ""
        # Cudzysłowy w tekście mogą złamać osascript — escapujemy
        safe_title = title.replace('"', '\\"')
        safe_message = message.replace('"', '\\"')
        script = f'display notification "{safe_message}" with title "{safe_title}" {sound_part}'
        result = subprocess.run(["osascript", "-e", script], capture_output=True)
        if result.returncode != 0:
            print(f"[mac notify błąd] {result.stderr.decode()}")

    try:
        req = urllib.request.Request(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": "4",
                "Tags": "billiards",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[ntfy błąd] {e}")

    print(f"\n🔔 POWIADOMIENIE: {title}\n   {message}\n")


def find_team_matches(data: dict, team_name: str) -> list[dict]:
    """Zwraca wszystkie mecze, w których uczestniczy szukany zespół (case-insensitive)."""
    team_lower = team_name.lower()
    return [
        m for m in data.get("matches", [])
        if team_lower in (m.get("playerA", {}).get("name", "") or "").lower()
        or team_lower in (m.get("playerB", {}).get("name", "") or "").lower()
    ]


def match_summary(match: dict, team_name: str) -> str:
    """Zwraca czytelny opis wyniku meczu."""
    a = match.get("playerA", {}).get("name", "?")
    b = match.get("playerB", {}).get("name", "?")
    sa = match.get("scoreA", 0)
    sb = match.get("scoreB", 0)
    race = match.get("raceTo", "?")
    status = match.get("matchstatus", "")
    round_name = match.get("roundName", "")

    team_lower = team_name.lower()
    is_a = team_lower in a.lower()

    my_score = sa if is_a else sb
    opp_score = sb if is_a else sa
    opponent = b if is_a else a

    if status == "finished":
        won = (is_a and sa > sb) or (not is_a and sb > sa)
        result = "wygrał" if won else "przegrał"
        return f"{round_name}: {team_name} {result} z {opponent} {my_score}:{opp_score} (Race to {race})"
    elif status == "playing":
        return f"{round_name}: {team_name} vs {opponent} — {my_score}:{opp_score} 🎱 (w trakcie)"
    else:
        return f"{round_name}: {team_name} vs {opponent} — oczekuje"


def has_future_match(tournament_data: dict, team_name: str) -> bool:
    """Sprawdza czy gracz ma jeszcze jakiś mecz scheduled/playing w turnieju."""
    team_lower = team_name.lower()
    for m in tournament_data.get("matches", []):
        if m.get("matchstatus") in ("finished",):
            continue
        a = (m.get("playerA", {}).get("name", "") or "").lower()
        b = (m.get("playerB", {}).get("name", "") or "").lower()
        if team_lower in a or team_lower in b:
            return True
    return False


def determine_advancement(match: dict, team_name: str, tournament_data: dict) -> str:
    """
    Analizuje co oznacza wynik meczu dla śledzionego zespołu.
    Zwraca None jeśli nic ważnego do zgłoszenia.
    """
    status = match.get("matchstatus", "")
    if status != "finished":
        return None

    a = match.get("playerA", {}).get("name", "")
    b = match.get("playerB", {}).get("name", "")
    sa = match.get("scoreA", 0)
    sb = match.get("scoreB", 0)
    team_lower = team_name.lower()
    is_a = team_lower in a.lower()
    won = (is_a and sa > sb) or (not is_a and sb > sa)

    round_name = match.get("roundName", "").lower()

    # Finał — obsługujemy zawsze natychmiast
    if "final" in round_name or "finale" in round_name:
        if won:
            return "MISTRZ TURNIEJU! 🏆"
        else:
            return "Finał przegrany — drugie miejsce 🥈"

    if "semi" in round_name or "półfinał" in round_name:
        if won:
            return "AWANS DO FINAŁU! 🎯"

    if "quarter" in round_name or "ćwierćfinał" in round_name:
        if won:
            return "AWANS DO PÓŁFINAŁU! ✅"

    if won:
        if "winner" in round_name or "wygranych" in round_name:
            return "Wygrana w drabince wygranych ✅"
        if "loser" in round_name or "przegranych" in round_name:
            return "Wygrana w drabince przegranych — gra dalej! ✅"
        return None  # Wygrana w grupie — bez komentarza

    # Przegrana — sprawdzamy CueScore czy gracz ma jeszcze mecz
    # (czekamy aż bracket się zaktualizuje zanim powiemy "eliminacja")
    if has_future_match(tournament_data, team_name):
        if "winner" in round_name or "wygranych" in round_name:
            return "Przegrana — spada do drabinki przegranych ⬇️"
        return None  # Ma kolejny mecz — nie strasz eliminacją
    else:
        # Brak przyszłych meczów w danych CueScore = potwierdzona eliminacja
        if "loser" in round_name or "przegranych" in round_name:
            return "ELIMINACJA z turnieju ❌"
        if re.search(r"round\s*\d+|grupa|group", round_name):
            return None  # Przegrana w grupie bez przyszłych meczów — może jeszcze nie wygenerowano
        return "ELIMINACJA z turnieju ❌"


def load_state() -> dict:
    """Wczytuje stan poprzedniej sesji z pliku."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict):
    """Zapisuje bieżący stan do pliku."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def print_team_status(matches: list[dict], team_name: str):
    """Wyświetla aktualny status wszystkich meczów zespołu."""
    if not matches:
        print(f"  Brak meczów dla zespołu '{team_name}'")
        return
    for m in matches:
        status = m.get("matchstatus", "unknown")
        icon = {"finished": "✅", "playing": "🎱", "scheduled": "⏳"}.get(status, "❓")
        print(f"  {icon} {match_summary(m, team_name)}")


def player_stats(data: dict, team_name: str) -> str:
    """Zwraca statystyki W/L gracza w turnieju (np. '3W 1L')."""
    wins = losses = 0
    team_lower = team_name.lower()
    for m in data.get("matches", []):
        if m.get("matchstatus") != "finished":
            continue
        a = (m.get("playerA", {}).get("name", "") or "").lower()
        b = (m.get("playerB", {}).get("name", "") or "").lower()
        if team_lower not in a and team_lower not in b:
            continue
        sa, sb = m.get("scoreA", 0), m.get("scoreB", 0)
        is_a = team_lower in a
        won = (is_a and sa > sb) or (not is_a and sb > sa)
        if won:
            wins += 1
        else:
            losses += 1
    return f"{wins}W {losses}L"


def process_team(match: dict, team_name: str, known: dict, data: dict, all_tracked: list):
    """Sprawdza zmiany dla jednego meczu jednego zespołu i wysyła powiadomienia."""
    mid = str(match.get("matchId"))
    cur_sa = match.get("scoreA", 0)
    cur_sb = match.get("scoreB", 0)
    cur_status = match.get("matchstatus", "")
    race_to = match.get("raceTo", 0)

    prev = known.get(mid, {})
    prev_sa = prev.get("scoreA", -1)
    prev_sb = prev.get("scoreB", -1)
    prev_status = prev.get("status", "")

    round_name = match.get("roundName", "")
    is_a = team_name.lower() in (match.get("playerA", {}).get("name", "") or "").lower()
    opponent = match.get("playerB", {}).get("name", "?") if is_a else match.get("playerA", {}).get("name", "?")
    my_score = cur_sa if is_a else cur_sb
    opp_score = cur_sb if is_a else cur_sa

    # Czy przeciwnik też jest śledzony?
    opp_tracked = any(t.lower() in opponent.lower() for t in all_tracked if t != team_name)
    heart = " 💚" if opp_tracked else ""

    # Ostatnia piłka: oboje mają race_to-1
    last_ball = (race_to > 0 and my_score == race_to - 1 and opp_score == race_to - 1)
    last_ball_tag = " 🔥 OSTATNIA PIŁKA!" if last_ball else ""

    if cur_status == "playing" and prev_status != "playing":
        stats = player_stats(data, team_name)
        notify(
            f"🎱 [{round_name}] Mecz się zaczął!{heart}",
            f"{team_name} ({stats}) vs {opponent}",
        )

    elif cur_status == "playing" and (cur_sa != prev_sa or cur_sb != prev_sb):
        prev_my = prev_sa if is_a else prev_sb
        prev_opp = prev_sb if is_a else prev_sa
        prev_last_ball = (race_to > 0 and prev_my == race_to - 1 and prev_opp == race_to - 1)
        # Wyślij "ostatnia piłka" tylko raz — gdy właśnie osiągnęliśmy ten stan
        if last_ball and not prev_last_ball:
            notify(
                f"⚡ [{round_name}] {my_score}:{opp_score}{last_ball_tag}",
                f"{team_name} vs {opponent}{heart}",
            )
        else:
            notify(
                f"🎱 [{round_name}] {team_name}: {my_score}:{opp_score}",
                f"vs {opponent}{heart}",
            )

    if cur_status == "finished" and prev_status != "finished":
        won = (is_a and cur_sa > cur_sb) or (not is_a and cur_sb > cur_sa)
        result = "wygrał ✅" if won else "przegrał ❌"
        advancement = determine_advancement(match, team_name, data)
        stats = player_stats(data, team_name)
        # Jeśli advancement to ELIMINACJA — nie wysyłaj od razu, zapisz do poczekalni
        # i poczekaj 2 minuty żeby CueScore zdążył zaktualizować drabinkę
        if advancement and "ELIMINACJA" in advancement:
            known["_pending_elim"] = {
                "matchId": mid, "opponent": opponent,
                "score": f"{my_score}:{opp_score}", "round": round_name,
                "stats": stats, "heart": heart,
                "check_after": time.time() + 120,  # sprawdź za 2 minuty
            }
            advancement = None
        else:
            known.pop("_pending_elim", None)
        notify(
            f"[{round_name}] {team_name} {result} ({stats}){heart}",
            f"{my_score}:{opp_score} vs {opponent}" + (f" — {advancement}" if advancement else ""),
        )

    known[mid] = {"scoreA": cur_sa, "scoreB": cur_sb, "status": cur_status}


def run_tracker(tournament_url: str, team_names: list[str]):
    """Główna pętla śledzenia wielu zespołów."""
    tournament_id = extract_tournament_id(tournament_url)
    if not tournament_id:
        print(f"❌ Nie można wyciągnąć ID turnieju z URL: {tournament_url}")
        return

    teams_str = ", ".join(team_names)
    print(f"\n{'='*60}")
    print(f"  CueScore Live Tracker")
    print(f"{'='*60}")
    print(f"  Turniej ID : {tournament_id}")
    print(f"  Śledzone   : {teams_str}")
    print(f"  Odświeżanie: co {POLL_INTERVAL}s")
    print(f"  Wyjście    : Ctrl+C")
    print(f"{'='*60}\n")

    full_state = load_state()
    # Osobny słownik stanu dla każdego zespołu
    known_per_team = {
        name: full_state.get(f"{tournament_id}_{name}", {}) for name in team_names
    }

    notify(
        "Tracker uruchomiony 🎱",
        f"Śledzę: {teams_str} | Turniej #{tournament_id}",
        sound=False,
    )

    while True:
        data = fetch_tournament(tournament_id)
        now = datetime.now().strftime("%H:%M:%S")

        if data is None:
            print(f"[{now}] Nie udało się pobrać danych, retry za {POLL_INTERVAL}s...")
            time.sleep(POLL_INTERVAL)
            continue

        for team_name in team_names:
            known = known_per_team[team_name]

            # Sprawdź poczekalnię eliminacji
            pending = known.get("_pending_elim")
            if pending:
                if has_future_match(data, team_name):
                    # Ma już kolejny mecz — fałszywy alarm, czyścimy
                    print(f"[{now}] {team_name}: anulowano fałszywą eliminację (ma kolejny mecz)")
                    known.pop("_pending_elim", None)
                elif time.time() >= pending["check_after"]:
                    # Minęły 2 minuty i nadal brak meczu — potwierdzamy eliminację
                    notify(
                        f"[{pending['round']}] ELIMINACJA z turnieju ❌{pending['heart']}",
                        f"{pending['score']} vs {pending['opponent']} | {pending['stats']}",
                    )
                    known.pop("_pending_elim", None)
                else:
                    remaining = int(pending["check_after"] - time.time())
                    print(f"[{now}] {team_name}: możliwa eliminacja, czekam jeszcze {remaining}s na aktualizację CueScore")

            matches = find_team_matches(data, team_name)
            if not matches:
                print(f"[{now}] Brak meczów dla '{team_name}'")
            else:
                print(f"[{now}] {team_name}:")
                print_team_status(matches, team_name)
                for match in matches:
                    process_team(match, team_name, known, data, team_names)

            full_state[f"{tournament_id}_{team_name}"] = known

        save_state(full_state)
        time.sleep(POLL_INTERVAL)


def interactive_setup():
    """Interaktywny tryb konfiguracji."""
    print("\n╔══════════════════════════════════════════════╗")
    print("║       CueScore Live Tracker 🎱               ║")
    print("╚══════════════════════════════════════════════╝\n")

    print(f"Domyślny URL: {DEFAULT_TOURNAMENT_URL}")
    url_input = input("Wklej link do turnieju (Enter = użyj domyślnego): ").strip()
    url = url_input if url_input else DEFAULT_TOURNAMENT_URL

    tid = extract_tournament_id(url)
    if not tid:
        print("❌ Nie rozpoznaję formatu URL CueScore.")
        sys.exit(1)

    print(f"\nPobieram dane turnieju {tid}...")
    data = fetch_tournament(tid)
    if not data:
        print("❌ Nie udało się pobrać danych turnieju.")
        sys.exit(1)

    print(f"✅ Turniej: {data.get('name', '?')}\n")

    # Pokaż wszystkie zespoły
    teams = set()
    for m in data.get("matches", []):
        if name := (m.get("playerA") or {}).get("name"):
            teams.add(name)
        if name := (m.get("playerB") or {}).get("name"):
            teams.add(name)

    if teams:
        print("Dostępne zespoły:")
        for i, t in enumerate(sorted(teams), 1):
            print(f"  {i:2}. {t}")

    team_names = []
    while True:
        prompt = "Wpisz nazwę zespołu (lub część nazwy): " if not team_names else "Dodaj kolejny zespół (Enter = gotowe): "
        print()
        team_name = input(prompt).strip()

        if not team_name:
            if not team_names:
                print("❌ Musisz podać co najmniej jeden zespół.")
                continue
            break

        test_matches = find_team_matches(data, team_name)
        if not test_matches:
            print(f"⚠️  Nie znaleziono meczów dla '{team_name}'. Sprawdź pisownię.")
            cont = input("Dodać mimo to? (t/n): ").strip().lower()
            if cont != "t":
                continue
        else:
            print(f"✅ '{team_name}' — znaleziono {len(test_matches)} mecz(e):")
            print_team_status(test_matches, team_name)

        team_names.append(team_name)

        if len(team_names) >= 10:
            print("(osiągnięto limit 10 zespołów)")
            break

    print()
    run_tracker(url, team_names)


def load_config() -> dict:
    """Wczytuje tracker_config.json jeśli istnieje."""
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main():
    # Tryb 1: argumenty z wiersza poleceń
    if len(sys.argv) >= 3:
        run_tracker(sys.argv[1], sys.argv[2:])
        return
    if len(sys.argv) == 2:
        tid = extract_tournament_id(sys.argv[1])
        if tid:
            team = input("Wpisz nazwę swojego zespołu: ").strip()
            run_tracker(sys.argv[1], [team])
        else:
            print("❌ Nieprawidłowy URL")
            sys.exit(1)
        return

    # Tryb 2: automatyczny — czyta tracker_config.json
    config = load_config()
    if config:
        tournaments = config.get("tournaments", [])

        # stary format (jeden turniej) — konwertuj w locie
        if not tournaments and config.get("tournament_url"):
            tournaments = [{"url": config["tournament_url"], "teams": config.get("teams", [])}]

        valid = [(t["url"], t["teams"]) for t in tournaments if t.get("url") and t.get("teams")]

        if valid:
            print(f"📋 Wczytuję konfigurację z tracker_config.json ({len(valid)} turniej/e)")
            for url, teams in valid:
                tid = extract_tournament_id(url)
                print(f"   #{tid}: {', '.join(teams)}")

            if len(valid) == 1:
                run_tracker(valid[0][0], valid[0][1])
            else:
                # Każdy turniej w osobnym wątku
                threads = []
                for url, teams in valid:
                    t = threading.Thread(target=run_tracker, args=(url, teams), daemon=True)
                    t.start()
                    threads.append(t)
                for t in threads:
                    t.join()
            return
        else:
            print("⚠️  tracker_config.json niekompletny — przechodzę do trybu interaktywnego")

    # Tryb 3: interaktywny
    interactive_setup()


if __name__ == "__main__":
    main()
