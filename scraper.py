"""
scraper.py - zurueckhaltender Abruf von Kleinanzeigen-Suchseiten.

Regeln, an die sich dieses Programm haelt:
  * Es laedt robots.txt und ruft nur Adressen ab, die dort fuer alle Roboter
    erlaubt sind. Verbotene Adressen werden uebersprungen und angezeigt.
  * Ist robots.txt nicht abrufbar, wird NICHTS abgerufen.
  * Es nennt sich ehrlich (User-Agent) und wartet zwischen den Abrufen.
  * Bei HTTP 401/403/429/503 oder einer Bot-Pruefung bricht es sofort ab.
    Es versucht nicht, eine Sperre zu umgehen.

Start:
  python scraper.py                 normaler Lauf (speichert, sendet Mail)
  python scraper.py --trocken       nur abrufen und auswerten, nichts speichern/senden
  python scraper.py --debug         zeigt je Anzeige, was erkannt wurde
  python scraper.py --html-ordner x speichert die geladenen Seiten (zur Fehlersuche)
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

import radar

LOGGER = logging.getLogger("scraper")

HOST = "www.kleinanzeigen.de"
BASIS = "https://" + HOST
PRODUKT = "schnaeppchen-radar"
USER_AGENT = PRODUKT + "/1.0 (privates Hobbyprojekt; wenige Abrufe, haelt sich an robots.txt)"

BOT_HINWEISE = re.compile(
    r"captcha|access denied|are you a (?:robot|human)|unusual traffic|bot.?protection|verify you are human",
    re.IGNORECASE,
)


class Gesperrt(Exception):
    """Kleinanzeigen verweigert den Zugriff, oder robots.txt verbietet ihn."""


# ==========================================================
# robots.txt (mit Platzhaltern * und $ wie bei Google)
# ==========================================================

class RobotsRegeln:
    def __init__(self, text: str, produkt: str = PRODUKT):
        self.regeln = []

        for typ, pfad in self._regeln_lesen(text, produkt):
            ende = pfad.endswith("$")
            muster = pfad[:-1] if ende else pfad
            teile = [re.escape(teil) for teil in muster.split("*")]
            regex = "^" + ".*".join(teile) + ("$" if ende else "")
            self.regeln.append((typ, len(pfad), re.compile(regex)))

    @staticmethod
    def _regeln_lesen(text: str, produkt: str) -> list:
        gruppen = []
        agenten = []
        regeln = []
        letzte_war_agent = False

        for zeile in text.splitlines():
            zeile = zeile.split("#", 1)[0].strip()

            if ":" not in zeile:
                continue

            feld, wert = zeile.split(":", 1)
            feld = feld.strip().lower()
            wert = wert.strip()

            if feld == "user-agent":
                if not letzte_war_agent:
                    if agenten:
                        gruppen.append((agenten, regeln))
                    agenten = []
                    regeln = []
                agenten.append(wert.lower())
                letzte_war_agent = True
            elif feld in ("allow", "disallow"):
                letzte_war_agent = False
                if wert == "" and feld == "disallow":
                    continue
                regeln.append((feld, wert))
            else:
                letzte_war_agent = False

        if agenten:
            gruppen.append((agenten, regeln))

        eigene = [r for a, r in gruppen if any(x != "*" and x in produkt.lower() for x in a)]
        allgemeine = [r for a, r in gruppen if "*" in a]

        ausgewaehlt = eigene if eigene else allgemeine

        ergebnis = []
        for gruppe in ausgewaehlt:
            ergebnis.extend(gruppe)

        return ergebnis

    def erlaubt(self, pfad_mit_query: str) -> bool:
        beste = None

        for typ, laenge, regex in self.regeln:
            if not regex.match(pfad_mit_query):
                continue

            if beste is None or laenge > beste[1] or (laenge == beste[1] and typ == "allow"):
                beste = (typ, laenge)

        return beste is None or beste[0] == "allow"

    def url_erlaubt(self, url: str) -> bool:
        teile = urlsplit(url)
        pfad = teile.path or "/"

        if teile.query:
            pfad += "?" + teile.query

        return self.erlaubt(pfad)


# ==========================================================
# Abruf
# ==========================================================

def neue_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": radar.env_wert("SCRAPER_USER_AGENT", erforderlich=False, standard=USER_AGENT),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "de-DE,de;q=0.9",
        }
    )
    return session


def robots_laden(session) -> RobotsRegeln:
    try:
        antwort = session.get(BASIS + "/robots.txt", timeout=20)
    except requests.RequestException as fehler:
        raise Gesperrt(f"robots.txt nicht abrufbar ({fehler}). Es wird nichts abgerufen.")

    if antwort.status_code != 200:
        raise Gesperrt(
            f"robots.txt nicht abrufbar (HTTP {antwort.status_code}). "
            "Es wird nichts abgerufen."
        )

    return RobotsRegeln(antwort.text)


def seite_holen(session, url: str, robots: RobotsRegeln) -> str:
    """Laedt eine Seite. Folgt Weiterleitungen nur, wenn das Ziel ebenfalls erlaubt ist."""
    aktuelle_url = url

    for _ in range(4):
        if urlsplit(aktuelle_url).netloc != HOST:
            raise Gesperrt(f"Weiterleitung auf fremden Host: {aktuelle_url}")

        if not robots.url_erlaubt(aktuelle_url):
            raise Gesperrt(f"robots.txt verbietet: {aktuelle_url}")

        antwort = session.get(aktuelle_url, timeout=25, allow_redirects=False)

        if antwort.status_code in (301, 302, 303, 307, 308):
            ziel = antwort.headers.get("Location", "")
            aktuelle_url = urljoin(aktuelle_url, ziel)
            continue

        if antwort.status_code in (401, 403, 429, 503):
            raise Gesperrt(
                f"HTTP {antwort.status_code} von Kleinanzeigen. "
                "Der Zugriff wird verweigert, der Lauf wird abgebrochen."
            )

        antwort.raise_for_status()

        text = antwort.text

        if "/s-anzeige/" not in text and BOT_HINWEISE.search(text[:20000]):
            raise Gesperrt("Die Antwort sieht nach einer Bot-Pruefung aus. Der Lauf wird abgebrochen.")

        return text

    raise Gesperrt("Zu viele Weiterleitungen.")


# ==========================================================
# Auswertung der Suchergebnis-Seite
# ==========================================================

def preistext_strikt(text: str) -> str:
    """
    Nur "Zu verschenken" gilt als gratis. Woerter wie "kostenlos" kommen in
    Beschreibungen vor ("Versand kostenlos") und wuerden sonst Bezahlartikel
    als Gratis-Treffer melden.
    """
    klein = text.lower()

    if "zu verschenken" in klein:
        return "Zu verschenken"

    treffer = radar.PREIS_PATTERN.search(text)

    if treffer:
        preis = treffer.group(0)

        if re.search(r"\bvb\b", klein[treffer.end(): treffer.end() + 20]):
            preis += " VB"

        return preis

    if re.search(r"\bvb\b", klein):
        return "VB"

    return ""


def kurzer_ort(text: str) -> str:
    ort = radar.ort_aus_text(text)

    if not ort:
        return ""

    teile = ort.split()

    return " ".join(teile[:2])   # PLZ und erstes Wort


def anzeigen_block(link) -> str:
    box = link.find_parent("article") or link.find_parent("li")

    if box is not None:
        return radar.text_bereinigen(box.get_text(" ", strip=True))

    return radar.umgebender_text(link)


def seite_parsen(html_text: str, basis_url: str) -> list:
    soup = BeautifulSoup(html_text, "html.parser")
    gefunden = {}

    for link in soup.find_all("a", href=True):
        href = urljoin(basis_url, str(link["href"]))
        treffer = radar.ANZEIGE_LINK_PATTERN.search(href)

        if not treffer:
            continue

        url = radar.sichere_url(treffer.group(0))
        anzeige_id = radar.extrahiere_anzeigen_id(url)
        titel = radar.titel_aus_link(link)
        block = anzeigen_block(link)
        preis = preistext_strikt(block)
        ort = kurzer_ort(block)

        vorhanden = gefunden.get(anzeige_id)

        if vorhanden is None:
            gefunden[anzeige_id] = radar.Anzeige(
                id=anzeige_id,
                url=url,
                titel=titel,
                preis_text=preis,
                ort=ort,
                gefunden_am=radar.aktuelle_zeit(),
            )
            continue

        if titel and len(titel) > len(vorhanden.titel) and "€" not in titel:
            vorhanden.titel = titel

        if preis and not vorhanden.preis_text:
            vorhanden.preis_text = preis

        if ort and not vorhanden.ort:
            vorhanden.ort = ort

    return [a for a in gefunden.values() if a.titel]


def verarbeite(anzeigen: list, config: dict, datenbank, praefixe: list) -> list:
    neue = []
    gesehen = set()

    for anzeige in anzeigen:
        if anzeige.id in gesehen:
            continue

        gesehen.add(anzeige.id)

        if not datenbank.ist_neu(anzeige.id):
            continue

        if praefixe and not any(anzeige.ort.startswith(p) for p in praefixe):
            continue

        if not radar.bewerten(anzeige, config):
            continue

        anzeige.nachrichtenvorschlag = radar.nachricht_bauen(anzeige, config)

        if datenbank.speichern(anzeige):
            neue.append(anzeige)

    def schluessel(a):
        preis = a.preis_eur if a.preis_eur is not None else 999999
        return (-a.prioritaet, preis, a.titel.lower())

    neue.sort(key=schluessel)

    return neue


# ==========================================================
# Programm
# ==========================================================

def debug_ausgeben(url: str, anzeigen: list, config: dict, praefixe: list) -> None:
    print(f"  Anzeigen erkannt: {len(anzeigen)}")

    for anzeige in anzeigen[:40]:
        kopie = radar.Anzeige(
            id=anzeige.id, url=anzeige.url, titel=anzeige.titel,
            preis_text=anzeige.preis_text, ort=anzeige.ort,
        )

        if praefixe and not any(anzeige.ort.startswith(p) for p in praefixe):
            urteil = "verworfen: PLZ ausserhalb der Region"
        elif radar.bewerten(kopie, config):
            urteil = f"TREFFER ({kopie.kategorie_name})"
        else:
            urteil = "verworfen (Preis, Ausschlusswort oder keine Kategorie)"

        print(f"   - {anzeige.titel[:55]!r} | {anzeige.preis_text or '-'} | {anzeige.ort or '-'} -> {urteil}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Zurueckhaltender Kleinanzeigen-Abruf")
    parser.add_argument("--trocken", action="store_true", help="nichts speichern oder senden")
    parser.add_argument("--debug", action="store_true", help="Erkennung je Anzeige ausgeben")
    parser.add_argument("--html-ordner", default="", help="geladene Seiten hier speichern")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    config = radar.lade_config()
    einstellungen = config.get("scraper", {}) or {}

    urls = [str(u).strip() for u in einstellungen.get("urls", []) if str(u).strip()]
    urls = [u for u in urls if urlsplit(u).netloc == HOST]
    urls = urls[: int(einstellungen.get("max_urls", 12))]

    praefixe = [str(p) for p in einstellungen.get("nur_plz_praefixe", [])]
    pause = float(einstellungen.get("pause_sekunden", 10))
    erstlauf_still = bool(einstellungen.get("erstlauf_still", True))

    if not urls:
        print("In config.yaml steht unter scraper: urls keine gueltige kleinanzeigen.de-Adresse.")
        return 3

    session = neue_session()

    try:
        robots = robots_laden(session)
    except Gesperrt as grund:
        print("ABBRUCH:", grund)
        return 2

    ordner = Path(args.html_ordner) if args.html_ordner else None

    if ordner:
        ordner.mkdir(parents=True, exist_ok=True)

    alle = []
    uebersprungen = 0
    abgerufen = 0

    for nummer, url in enumerate(urls, start=1):
        if not robots.url_erlaubt(url):
            uebersprungen += 1
            print(f"[{nummer}] UEBERSPRUNGEN (robots.txt verbietet): {url}")
            continue

        if abgerufen:
            time.sleep(pause)

        try:
            html_text = seite_holen(session, url, robots)
        except Gesperrt as grund:
            print(f"[{nummer}] ABBRUCH:", grund)
            return 2
        except requests.RequestException as fehler:
            print(f"[{nummer}] Netzwerkfehler: {fehler}")
            continue

        abgerufen += 1

        if ordner:
            (ordner / f"seite_{nummer}.html").write_text(html_text, encoding="utf-8")

        anzeigen = seite_parsen(html_text, url)
        alle.extend(anzeigen)

        print(f"[{nummer}] {len(anzeigen)} Anzeigen auf {url}")

        if args.debug:
            debug_ausgeben(url, anzeigen, config, praefixe)

        if not anzeigen:
            print("    Warnung: Keine Anzeigen erkannt. Das Seitenlayout passt eventuell nicht zum Parser.")

    if abgerufen == 0:
        print("Es wurde keine Adresse abgerufen (alle durch robots.txt verboten oder nicht erreichbar).")
        return 3

    if args.trocken:
        print(f"Trockenlauf: {len(alle)} Anzeigen gesehen, nichts gespeichert oder gesendet.")
        return 0

    datenbank = radar.Datenbank()

    try:
        neue = verarbeite(alle, config, datenbank, praefixe)

        erstlauf = datenbank.status_lesen("scraper_gestartet", "") == ""

        if erstlauf and erstlauf_still:
            datenbank.als_versendet_markieren(neue)
            datenbank.status_schreiben("scraper_gestartet", "1")
            print(f"Erster Lauf: {len(neue)} bereits vorhandene Treffer als bekannt gespeichert, keine Mail.")
            return 0

        datenbank.status_schreiben("scraper_gestartet", "1")
        print(f"Neue Treffer: {len(neue)}")

        versendet = radar.offene_zusammenfassung_senden(datenbank, config, sofort=True)
        print(f"Per E-Mail versendet: {versendet}")
    finally:
        datenbank.schliessen()

    return 0


if __name__ == "__main__":
    sys.exit(main())
