# evcc to OCPP 1.6-J Bridge

Eine schlanke, bidirektionale Übersetzer-Brücke (**OCPP 1.6-J Bridge**), die eine von [evcc](https://evcc.io) gesteuerte Wallbox (z. B. openWB, go-e, cFos) an professionelle CPO- und THG-Abrechnungsbackends wie **PUMP connect** anbindet.

## Funktionsweise

evcc agiert lokal als Master für PV-Überschuss, Hausbatterie-Priorisierung und Lastmanagement. Die Bridge simuliert gegenüber externen CPO-Backends eine vollwertige OCPP-1.6-J-Ladestation:
- **RemoteStart (Gast/Kunde):** Schaltet evcc verzögerungsfrei auf Modus `now` (Schnellladen).
- **RemoteStop:** Schaltet die Wallbox auf `off`, wartet auf das Erreichen von 0 W und beendet die Transaktion sauber.
- **Abrechnungssicherheit:** Übergibt den absoluten Hardware-Zählerstand (`chargeTotalImport`) für `meterStart`, `meterStop` und zyklische `MeterValues` in Wattstunden (Wh).
- **Gast-Schutz:** Nach einem Fernstopp verbleibt der Ladepunkt auf `off`, solange das Fremdfahrzeug angesteckt bleibt (kein Gratis-Weiterladen).
- **Privatwagen-Rückfall:** Sobald ein Fahrzeug getrennt und neu angesteckt wird, schaltet die Bridge automatisch zurück auf `pv` (Smart-Modus).
- **Verfügbarkeit:** Meldet `Available` nur bei tatsächlich freiem Ladeplatz (`connected: False`). Angesteckte Fahrzeuge im Ruhezustand werden standardkonform als `SuspendedEVSE` gemeldet.

## Voraussetzungen

- Linux (z. B. Debian/Ubuntu LXC Container)
- Python 3.10+
- Laufende evcc-Instanz mit erreichbarer REST-API

## Schnellstart

1. Repository klonen:
   git clone [https://github.com/Mountain-wg/evcc-ocpp-bridge.git](https://github.com/Mountain-wg/evcc-ocpp-bridge.git) /opt/evcc-ocpp-bridge
   cd /opt/evcc-ocpp-bridge

2. Virtual Environment einrichten:
   python3 -m venv venv
   ./venv/bin/pip install --upgrade pip
   ./venv/bin/pip install -r requirements.txt

3. Konfiguration hinterlegen:
   cp .env.example .env
   nano .env

4. Dienst einrichten und starten:
   cp evcc-ocpp-bridge.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now evcc-ocpp-bridge.service

## Lizenz

MIT License
