from __future__ import annotations

import argparse
import logging
import sys
import time

from pathlib import Path

import radar


def utf8_aktivieren() -> None:
    try:
        sys.stdout.reconfigure(
            encoding="utf-8"
        )
    except Exception:
        pass

    try:
        sys.stderr.reconfigure(
            encoding="utf-8"
        )
    except Exception:
        pass


def parser_erstellen() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Schnäppchen-Radar für Kleinanzeigen"
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Ausführliche Debug-Ausgaben",
    )

    befehle = parser.add_subparsers(
        dest="befehl",
        required=True,
    )

    run = befehle.add_parser(
        "run",
        help="Gmail-Postfach prüfen",
    )
    run.add_argument(
        "--alle",
        action="store_true",
        help="Auch bereits gelesene Mails prüfen",
    )
    run.add_argument(
        "--sofort-senden",
        action="store_true",
        help="Offene Treffer sofort per E-Mail senden",
    )
    run.add_argument(
        "--dauerbetrieb",
        action="store_true",
        help="Programm dauerhaft laufen lassen",
    )
    run.add_argument(
        "--loop",
        type=int,
        default=None,
        help="Prüfintervall in Sekunden",
    )

    datei = befehle.add_parser(
        "datei",
        help="HTML-Testmail analysieren",
    )
    datei.add_argument(
        "pfad",
        help="Pfad zur HTML-Datei",
    )
    datei.add_argument(
        "--senden",
        action="store_true",
        help="Gefundene Testtreffer sofort senden",
    )

    befehle.add_parser(
        "test-email",
        help="E-Mail-Versand testen",
    )

    befehle.add_parser(
        "senden",
        help="Offene Treffer sofort senden",
    )

    letzte = befehle.add_parser(
        "letzte",
        help="Letzte gespeicherte Treffer anzeigen",
    )
    letzte.add_argument(
        "--anzahl",
        type=int,
        default=20,
    )

    befehle.add_parser(
        "status",
        help="Programmstatus anzeigen",
    )

    return parser


def main() -> None:
    utf8_aktivieren()

    parser = parser_erstellen()
    args = parser.parse_args()

    logging.basicConfig(
        level=(
            logging.DEBUG
            if args.verbose
            else logging.INFO
        ),
        format=(
            "%(asctime)s "
            "%(levelname)-8s "
            "%(name)s: "
            "%(message)s"
        ),
        datefmt="%H:%M:%S",
    )

    config = radar.lade_config()
    datenbank = radar.Datenbank()

    try:
        if args.befehl == "test-email":
            radar.test_email_senden(
                config
            )
            print(
                "Test-E-Mail wurde erfolgreich verschickt."
            )

        elif args.befehl == "datei":
            pfad = Path(args.pfad)

            if not pfad.exists():
                raise FileNotFoundError(
                    f"Datei nicht gefunden: {pfad}"
                )

            html_text = pfad.read_text(
                encoding="utf-8",
                errors="replace",
            )

            treffer = radar.treffer_speichern(
                [html_text],
                config,
                datenbank,
            )

            print(
                f"{len(treffer)} neue Testtreffer gespeichert."
            )

            if args.senden:
                versendet = (
                    radar.offene_zusammenfassung_senden(
                        datenbank,
                        config,
                        sofort=True,
                    )
                )
                print(
                    f"{versendet} Treffer versendet."
                )

        elif args.befehl == "senden":
            versendet = (
                radar.offene_zusammenfassung_senden(
                    datenbank,
                    config,
                    sofort=True,
                )
            )

            print(
                f"{versendet} Treffer versendet."
            )

        elif args.befehl == "run":
            intervall = (
                args.loop
                if args.loop is not None
                else int(
                    config.get(
                        "pruefung_alle_minuten",
                        5,
                    )
                ) * 60
            )

            while True:
                try:
                    neue, versendet = (
                        radar.einmal_pruefen(
                            config,
                            datenbank,
                            nur_ungelesen=not args.alle,
                            sofort_senden=args.sofort_senden,
                        )
                    )

                    print(
                        f"Neue Treffer: {neue}"
                    )
                    print(
                        f"Versendet: {versendet}"
                    )
                    print(
                        "Offene Treffer:",
                        datenbank.offene_anzahl(),
                    )

                except Exception as fehler:
                    logging.exception(
                        "Prüfung fehlgeschlagen: %s",
                        fehler,
                    )

                    if not args.dauerbetrieb:
                        raise SystemExit(1)

                if not args.dauerbetrieb:
                    break

                print(
                    f"Nächste Prüfung in "
                    f"{intervall} Sekunden."
                )

                time.sleep(intervall)

        elif args.befehl == "letzte":
            zeilen = datenbank.letzte_anzeigen(
                args.anzahl
            )

            if not zeilen:
                print(
                    "Noch keine Anzeigen gespeichert."
                )

            for zeile in zeilen:
                status = (
                    "versendet"
                    if zeile["versendet_am"]
                    else "offen"
                )

                print()
                print("=" * 70)
                print(
                    "Gefunden:",
                    radar.formatiere_zeit(
                        zeile["gefunden_am"]
                    ),
                )
                print(
                    "Status:",
                    status,
                )
                print(
                    "Kategorie:",
                    zeile["kategorie_name"],
                )
                print(
                    "Titel:",
                    zeile["titel"],
                )
                print(
                    "Preis:",
                    zeile["preis_text"],
                )
                print(
                    "Ort:",
                    zeile["ort"],
                )
                print(
                    "URL:",
                    zeile["url"],
                )

        elif args.befehl == "status":
            print(
                "Offene Treffer:",
                datenbank.offene_anzahl(),
            )

            letzter = datenbank.status_lesen(
                "letzter_versand",
                "0",
            )

            if letzter != "0":
                print(
                    "Letzter Versand:",
                    radar.formatiere_zeit(
                        int(letzter)
                    ),
                )
            else:
                print(
                    "Letzter Versand: noch keiner"
                )

    finally:
        datenbank.schliessen()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nProgramm beendet."
        )