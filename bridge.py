import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
import requests
import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AvailabilityStatus,
    ChargePointStatus,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    UnlockStatus,
)

__VERSION__ = "v1.2.7"
__MODEL__ = "evcc OCPP Bridge"
__VENDOR__ = "evcc (Jermyn)"  # Max. 20 Zeichen (OCPP CiString20Type)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("evcc-ocpp-bridge")

# OCPP-URL: Bevorzugt OCPP_BACKEND_URL, Fallback auf PUMP_OCPP_URL für Abwärtskompatibilität
OCPP_BACKEND_URL = os.getenv("OCPP_BACKEND_URL") or os.getenv("PUMP_OCPP_URL")
EVCC_API_URL = os.getenv("EVCC_API_URL", "http://127.0.0.1:7070").rstrip("/")
LOADPOINT_ID = os.getenv("EVCC_LOADPOINT_ID", "1")
METER_INTERVAL = int(os.getenv("METER_INTERVAL", "60"))


class EvccOcppBridge(cp):
    def __init__(self, id, connection):
        super().__init__(id, connection)
        self.is_charging = False
        self.was_connected = False
        self.heartbeat_interval = 60
        self.active_transaction_id = None
        self.current_id_tag = "AUTO_TAG"
        self.remote_session_active = False
        self.last_reported_status = None

    def get_evcc_state(self):
        try:
            r = requests.get(f"{EVCC_API_URL}/api/state", timeout=5)
            r.raise_for_status()
            data = r.json()
            state = data.get("result", data) if isinstance(data, dict) else {}
            loadpoints = state.get("loadpoints", [])
            idx = int(LOADPOINT_ID) - 1
            if 0 <= idx < len(loadpoints):
                return loadpoints[idx]
        except Exception as e:
            logger.error(f"Fehler beim Abruf von evcc: {e}")
        return None

    def extract_meter_values(self, lp):
        if not lp:
            return 0.0, 0.0

        energy_kwh = (
            lp.get("chargeTotalImport")
            or lp.get("chargeMeterTotal")
            or lp.get("meterEnergy")
            or 0.0
        )
        power_w = lp.get("chargePower") or 0.0
        return float(energy_kwh), float(power_w)

    def set_evcc_mode(self, mode: str):
        try:
            r = requests.post(
                f"{EVCC_API_URL}/api/loadpoints/{LOADPOINT_ID}/mode/{mode}",
                timeout=5,
            )
            r.raise_for_status()
            logger.info(f"evcc Modus auf '{mode}' gesetzt.")
            return True
        except Exception as e:
            logger.error(f"Konnte evcc Modus nicht setzen: {e}")
            return False

    def unlock_evcc_connector(self):
        try:
            r = requests.post(
                f"{EVCC_API_URL}/api/loadpoints/{LOADPOINT_ID}/unlock",
                timeout=5,
            )
            return r.status_code == 200
        except Exception as e:
            logger.warning(f"Konnte Connector via evcc nicht entriegeln: {e}")
            return False

    # --- Eingehende OCPP 1.6-J Handler ---

    @on(Action.remote_start_transaction)
    async def on_remote_start_transaction(self, id_tag, connector_id=1, **kwargs):
        logger.info(f"OCPP Backend sendet RemoteStart (idTag: {id_tag})")
        self.current_id_tag = id_tag
        self.remote_session_active = True
        success = self.set_evcc_mode("now")

        if success:
            asyncio.create_task(self._handle_immediate_start())
            return call_result.RemoteStartTransaction(
                status=RemoteStartStopStatus.accepted
            )
        else:
            self.remote_session_active = False
            return call_result.RemoteStartTransaction(
                status=RemoteStartStopStatus.rejected
            )

    async def _handle_immediate_start(self):
        await self.send_status(ChargePointStatus.preparing)
        lp = self.get_evcc_state()
        energy_kwh, _ = self.extract_meter_values(lp)
        await self.start_transaction(energy_kwh)
        self.is_charging = True
        await self.send_status(ChargePointStatus.charging)

    @on(Action.remote_stop_transaction)
    async def on_remote_stop_transaction(self, transaction_id, **kwargs):
        logger.info(f"OCPP Backend sendet RemoteStop (TransID: {transaction_id})")
        asyncio.create_task(self._handle_immediate_stop())
        return call_result.RemoteStopTransaction(
            status=RemoteStartStopStatus.accepted
        )

    async def _handle_immediate_stop(self):
        # Sofort auf 'off' schalten, um Nachlaufstrom zu sperren
        self.set_evcc_mode("off")

        # Warten bis Ladeleistung tatsächlich auf 0 W fällt (max. 10 s)
        for _ in range(10):
            await asyncio.sleep(1)
            lp = self.get_evcc_state()
            _, power_w = self.extract_meter_values(lp)
            if power_w == 0.0:
                break

        lp = self.get_evcc_state()
        energy_kwh, _ = self.extract_meter_values(lp)
        await self.stop_transaction(energy_kwh)
        self.is_charging = False
        self.remote_session_active = False

        connected = lp.get("connected", False) if lp else False
        next_status = (
            ChargePointStatus.suspended_evse
            if connected
            else ChargePointStatus.available
        )
        await self.send_status(next_status)
        logger.info(
            f"Ladung beendet. Modus bleibt 'off' bis Trennung. Status: {next_status}"
        )

    @on(Action.reset)
    async def on_reset(self, type: ResetType, **kwargs):
        logger.warning(f"OCPP Backend fordert Reset an (Typ: {type}). Quittiere accepted...")
        loop = asyncio.get_running_loop()
        loop.call_later(2, lambda: sys.exit(0))
        return call_result.Reset(status=ResetStatus.accepted)

    @on(Action.unlock_connector)
    async def on_unlock_connector(self, connector_id, **kwargs):
        logger.info(f"OCPP Backend fordert Entriegelung von Connector {connector_id}")
        unlocked = self.unlock_evcc_connector()
        status = (
            UnlockStatus.unlocked
            if unlocked
            else UnlockStatus.unlock_failed
        )
        return call_result.UnlockConnector(status=status)

    @on(Action.clear_charging_profile)
    async def on_clear_charging_profile(self, **kwargs):
        return call_result.ClearChargingProfile(
            status=ClearChargingProfileStatus.accepted
        )

    @on(Action.change_availability)
    async def on_change_availability(self, connector_id, type, **kwargs):
        status = (
            ChargePointStatus.available
            if type == "Operative"
            else ChargePointStatus.unavailable
        )
        asyncio.create_task(self.send_status(status))
        return call_result.ChangeAvailability(status=AvailabilityStatus.accepted)

    @on(Action.get_configuration)
    async def on_get_configuration(self, key=None, **kwargs):
        configuration_list = [
            {"key": "HeartbeatInterval", "readonly": False, "value": str(self.heartbeat_interval)},
            {"key": "MeterValueSampleInterval", "readonly": False, "value": str(METER_INTERVAL)},
            {"key": "NumberOfConnectors", "readonly": True, "value": "1"},
        ]
        if key:
            configuration_list = [c for c in configuration_list if c["key"] in key]
        return call_result.GetConfiguration(
            configuration_key=configuration_list, unknown_key=[]
        )

    @on(Action.change_configuration)
    async def on_change_configuration(self, key, value, **kwargs):
        if key == "HeartbeatInterval":
            try:
                self.heartbeat_interval = int(value)
            except ValueError:
                pass
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)

    @on(Action.trigger_message)
    async def on_trigger_message(self, requested_message, connector_id=1, **kwargs):
        if requested_message == "StatusNotification":
            if self.last_reported_status:
                asyncio.create_task(self.send_status(self.last_reported_status))
        elif requested_message == "MeterValues":
            lp = self.get_evcc_state()
            if lp:
                energy_kwh, power_w = self.extract_meter_values(lp)
                asyncio.create_task(self.send_meter_values(energy_kwh, power_w))
        elif requested_message == "Heartbeat":
            asyncio.create_task(self.send_heartbeat())
        return call_result.TriggerMessage(status="Accepted")

    # --- Ausgehende OCPP-Meldungen ---

    async def send_boot_notification(self):
        req = call.BootNotification(
            charge_point_vendor=__VENDOR__,
            charge_point_model=__MODEL__,
            firmware_version=__VERSION__,
        )
        res = await self.call(req)
        logger.info(
            f"BootNotification bestätigt ({__VENDOR__} - {__MODEL__} {__VERSION__}): {res.status}"
        )

    async def send_heartbeat(self):
        try:
            req = call.Heartbeat()
            res = await self.call(req)
            logger.debug(f"Heartbeat gesendet. Backend-Zeit: {res.current_time}")
        except Exception as e:
            logger.warning(f"Heartbeat Übertragungsfehler: {e}")

    async def send_status(self, status: ChargePointStatus):
        self.last_reported_status = status
        req = call.StatusNotification(
            connector_id=1,
            error_code="NoError",
            status=status,
        )
        await self.call(req)
        logger.info(f"Status an Backend gemeldet: {status}")

    async def start_transaction(self, energy_kwh: float):
        now = datetime.now(timezone.utc).isoformat()
        wh = int(energy_kwh * 1000)
        req = call.StartTransaction(
            connector_id=1,
            id_tag=self.current_id_tag,
            meter_start=wh,
            timestamp=now,
        )
        res = await self.call(req)
        self.active_transaction_id = res.transaction_id
        logger.info(
            f"StartTransaction bestätigt. TransID: {self.active_transaction_id} (meter_start: {wh} Wh)"
        )

    async def stop_transaction(self, energy_kwh: float):
        if self.active_transaction_id is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        wh = int(energy_kwh * 1000)
        req = call.StopTransaction(
            meter_stop=wh,
            timestamp=now,
            transaction_id=self.active_transaction_id,
            id_tag=self.current_id_tag,
        )
        await self.call(req)
        logger.info(
            f"StopTransaction für TransID {self.active_transaction_id} (meter_stop: {wh} Wh) gesendet."
        )
        self.active_transaction_id = None

    async def send_meter_values(self, energy_kwh: float, power_w: float):
        now = datetime.now(timezone.utc).isoformat()
        wh = int(energy_kwh * 1000)
        samples = [
            {
                "value": str(wh),
                "context": "Sample.Periodic",
                "measurand": "Energy.Active.Import.Register",
                "unit": "Wh",
            },
            {
                "value": str(int(power_w)),
                "context": "Sample.Periodic",
                "measurand": "Power.Active.Import",
                "unit": "W",
            },
        ]
        kwargs = {
            "connector_id": 1,
            "meter_value": [{"timestamp": now, "sampledValue": samples}],
        }
        if self.active_transaction_id is not None:
            kwargs["transaction_id"] = self.active_transaction_id

        req = call.MeterValues(**kwargs)
        await self.call(req)

    # --- Periodische Tasks ---

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            await self.send_heartbeat()

    async def monitor_loop(self):
        while True:
            await asyncio.sleep(METER_INTERVAL)
            lp = self.get_evcc_state()
            if not lp:
                continue

            connected = lp.get("connected", False)
            charging = lp.get("charging", False)
            mode = lp.get("mode", "")
            energy_kwh, power_w = self.extract_meter_values(lp)

            # 1. Erkennung: Auto frisch angesteckt
            if connected and not self.was_connected:
                logger.info("Fahrzeug eingesteckt (connected: True).")
                if not self.remote_session_active and mode != "pv":
                    logger.info("Schalte für Privatfahrzeug automatisch auf 'pv'.")
                    self.set_evcc_mode("pv")

            # 2. Erkennung: Auto abgesteckt
            if not connected and self.was_connected:
                logger.info("Fahrzeug abgesteckt (connected: False).")

            self.was_connected = connected

            # 3. Berechne korrekten OCPP Connector-Status
            if charging:
                expected_status = ChargePointStatus.charging
            elif connected:
                expected_status = ChargePointStatus.suspended_evse
            else:
                expected_status = ChargePointStatus.available

            if expected_status != self.last_reported_status:
                await self.send_status(expected_status)

            try:
                await self.send_meter_values(energy_kwh, power_w)
            except Exception as e:
                logger.warning(f"MeterValues Übertragungsfehler: {e}")


async def main():
    if not OCPP_BACKEND_URL:
        logger.error("OCPP_BACKEND_URL fehlt in den Umgebungsvariablen!")
        sys.exit(1)

    cp_id = OCPP_BACKEND_URL.rstrip("/").split("/")[-1]
    retry_delay = 30

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown():
        logger.info("Shutdown-Signal empfangen. Schließe Verbindung...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    while not stop_event.is_set():
        try:
            logger.info(
                f"Verbinde zu Backend: {OCPP_BACKEND_URL} (Version {__VERSION__}) ..."
            )
            async with websockets.connect(
                OCPP_BACKEND_URL,
                subprotocols=["ocpp1.6"],
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ) as ws:
                charge_point = EvccOcppBridge(cp_id, ws)
                listen_task = asyncio.create_task(charge_point.start())
                await charge_point.send_boot_notification()

                # Initiale Statusevaluation
                initial_lp = charge_point.get_evcc_state()
                initial_connected = initial_lp.get("connected", False) if initial_lp else False
                initial_status = (
                    ChargePointStatus.suspended_evse
                    if initial_connected
                    else ChargePointStatus.available
                )
                await charge_point.send_status(initial_status)

                monitor_task = asyncio.create_task(charge_point.monitor_loop())
                hb_task = asyncio.create_task(charge_point.heartbeat_loop())
                retry_delay = 30

                done, pending = await asyncio.wait(
                    [
                        listen_task,
                        monitor_task,
                        hb_task,
                        asyncio.create_task(stop_event.wait()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()

                if stop_event.is_set():
                    await ws.close(reason="Bridge shutdown")
                    break

        except (websockets.ConnectionClosed, ConnectionRefusedError) as e:
            logger.warning(f"Verbindung getrennt ({e}). Reconnect in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay + 30, 120)
        except Exception as e:
            logger.error(f"Unerwarteter Fehler: {e}. Retry in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay + 30, 120)


if __name__ == "__main__":
    asyncio.run(main())
