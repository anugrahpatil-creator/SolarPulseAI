"""
app/scripts/seed_data.py

Seeds the database with demo roles, an admin user, and 3 real large-scale
Indian solar plants with realistic configurations, 48-hour forecast curves,
SCADA telemetry, and anomaly alerts.

Plants seeded
-------------
1. Bhadla Solar Park - Block A        (Rajasthan,  2,245 MW site)
2. Pavagada Solar Park - Sector 2     (Karnataka,  2,050 MW site)
3. Kamuthi Solar Power Project        (Tamil Nadu,   648 MW site)

Usage:
    python -m app.scripts.seed_data
"""

import asyncio
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.database import AsyncSessionLocal, init_db
from app.core.logging import get_logger
from app.core.security import hash_password
from app.models.alert import AnomalyAlert
from app.models.forecast import ForecastRecord
from app.models.plant import Plant, PlantConfig
from app.models.scada import ScadaReading
from app.models.user import Role, User

logger = get_logger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────


def generate_diurnal_power(hour_float: float, peak_kw: float) -> float:
    """Realistic solar generation curve: half-sine between sunrise and sunset."""
    sunrise, sunset = 6.0, 18.5
    if hour_float <= sunrise or hour_float >= sunset:
        return 0.0
    phase = (hour_float - sunrise) / (sunset - sunrise) * math.pi
    return max(0.0, round(peak_kw * math.sin(phase) ** 1.3, 2))


# ── Real Indian solar plant definitions ───────────────────────────────────────

DEMO_PLANTS = [
    # ── 1. Bhadla Solar Park - Block A (Rajasthan) ───────────────────────────
    dict(
        name="Bhadla Solar Park - Block A",
        location="Phalodi, Rajasthan, India",
        latitude=27.5385,
        longitude=71.9161,
        timezone="Asia/Kolkata",
        capacity_kw=50_000.0,
        inverter_capacity_kw=45_000.0,
        module_count=130_000,
        config=dict(tilt=26.0, azimuth=180.0, efficiency=0.195,
                    soiling_threshold=5.0, clipping_threshold=95.0,
                    forecast_horizon_minutes=60),
        peak_gen_kw=43_200.0,
        base_temp_c=38.0,
        alerts=[
            dict(alert_type="shortfall", severity="CRITICAL",
                 message="Inverter Station 3 trip — 12.4 MW sudden deficit during high irradiance",
                 expected=42_500.0, actual=30_100.0, deviation=29.18,
                 hours_ago=2.25, resolved=False),
            dict(alert_type="soiling", severity="WARNING",
                 message="Sub-array 4B soiling deficit — 14.8% shortfall vs clear-sky model",
                 expected=38_200.0, actual=32_550.0, deviation=14.79,
                 hours_ago=5.0, resolved=False),
        ],
    ),
    # ── 2. Pavagada Solar Park - Sector 2 (Karnataka) ────────────────────────
    dict(
        name="Pavagada Solar Park - Sector 2",
        location="Tumakuru, Karnataka, India",
        latitude=14.1030,
        longitude=77.2794,
        timezone="Asia/Kolkata",
        capacity_kw=25_000.0,
        inverter_capacity_kw=22_500.0,
        module_count=65_000,
        config=dict(tilt=15.0, azimuth=180.0, efficiency=0.202,
                    soiling_threshold=4.5, clipping_threshold=95.0,
                    forecast_horizon_minutes=60),
        peak_gen_kw=21_800.0,
        base_temp_c=32.0,
        alerts=[
            dict(alert_type="clipping", severity="WARNING",
                 message="AC clipping on Inverters 1-4 at 22.5 MW threshold",
                 expected=23_800.0, actual=22_500.0, deviation=5.46,
                 hours_ago=1.17, resolved=False),
        ],
    ),
    # ── 3. Kamuthi Solar Power Project (Tamil Nadu) ───────────────────────────
    dict(
        name="Kamuthi Solar Power Project",
        location="Kamuthi, Tamil Nadu, India",
        latitude=9.3691,
        longitude=78.7750,
        timezone="Asia/Kolkata",
        capacity_kw=648_000.0,
        inverter_capacity_kw=580_000.0,
        module_count=2_500_000,
        config=dict(tilt=11.0, azimuth=180.0, efficiency=0.188,
                    soiling_threshold=4.0, clipping_threshold=96.0,
                    forecast_horizon_minutes=60),
        peak_gen_kw=590_000.0,
        base_temp_c=34.0,
        alerts=[
            dict(alert_type="soiling", severity="WARNING",
                 message="Monsoon dust accumulation — row 14 irradiance 18% below baseline",
                 expected=560_000.0, actual=459_200.0, deviation=18.0,
                 hours_ago=3.0, resolved=False),
            dict(alert_type="curtailment", severity="INFO",
                 message="TANGEDCO scheduled curtailment resolved — nominal output restored",
                 expected=540_000.0, actual=540_000.0, deviation=0.0,
                 hours_ago=26.0, resolved=True),
        ],
    ),
]


async def seed() -> None:
    logger.info("Initializing tables before seeding...")
    await init_db()

    async with AsyncSessionLocal() as session:
        # ── 1. Roles ──────────────────────────────────────────────────────────
        role_admin = await session.scalar(select(Role).where(Role.name == "admin"))
        if not role_admin:
            role_admin = Role(name="admin")
            role_operator = Role(name="operator")
            role_viewer = Role(name="viewer")
            session.add_all([role_admin, role_operator, role_viewer])
            await session.flush()
            logger.info("Created roles: admin, operator, viewer")

        # ── 2. Admin User ─────────────────────────────────────────────────────
        admin_user = await session.scalar(
            select(User).where(User.email == "admin@solarpulse.ai")
        )
        if not admin_user:
            admin_user = User(
                username="admin",
                email="admin@solarpulse.ai",
                hashed_password=hash_password("admin12345"),
                full_name="SolarPulse Administrator",
                role_id=role_admin.id,
                is_active=True,
            )
            session.add(admin_user)
            logger.info("Created admin user: admin@solarpulse.ai / admin12345")

        # ── 3. Demo Plants ────────────────────────────────────────────────────
        now = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)

        # Remove any plants that are NOT in the current DEMO_PLANTS list.
        # This handles stale data from previous seeds (e.g. the old 7 extra plants).
        canonical_names = set()
        for pd_entry in DEMO_PLANTS:
            canonical_names.add(pd_entry["name"])
            canonical_names.add(pd_entry["name"].replace(" - ", " – "))
            canonical_names.add(pd_entry["name"].replace(" – ", " - "))

        all_plants = (await session.execute(select(Plant))).scalars().all()
        for stale in all_plants:
            if stale.name not in canonical_names:
                logger.info(f"  Removing stale plant [{stale.id}]: {stale.name}")
                await session.delete(stale)
        await session.flush()

        for idx, pd in enumerate(DEMO_PLANTS):
            plant = await session.scalar(
                select(Plant).where(
                    (Plant.name == pd["name"])
                    | (Plant.name == pd["name"].replace(" - ", " – "))
                    | (Plant.name == pd["name"].replace(" – ", " - "))
                )
            )

            if not plant:
                plant = Plant(
                    name=pd["name"],
                    location=pd["location"],
                    latitude=pd["latitude"],
                    longitude=pd["longitude"],
                    timezone=pd["timezone"],
                    capacity_kw=pd["capacity_kw"],
                    inverter_capacity_kw=pd["inverter_capacity_kw"],
                    module_count=pd["module_count"],
                    is_active=True,
                )
                session.add(plant)
                await session.flush()

                cfg = pd["config"]
                session.add(PlantConfig(
                    plant_id=plant.id,
                    tilt=cfg["tilt"],
                    azimuth=cfg["azimuth"],
                    efficiency=cfg["efficiency"],
                    soiling_threshold=cfg["soiling_threshold"],
                    clipping_threshold=cfg["clipping_threshold"],
                    forecast_horizon_minutes=cfg["forecast_horizon_minutes"],
                ))
                logger.info(f"  Created plant [{plant.id}]: {plant.name}")
            else:
                logger.info(f"  Existing plant [{plant.id}]: {plant.name}")

            # ── Forecast records (48 h window) ────────────────────────────────
            existing_fc = await session.scalar(
                select(ForecastRecord.id).where(ForecastRecord.plant_id == plant.id)
            )
            if not existing_fc:
                peak_kw = pd["peak_gen_kw"]
                fc_batch = []
                for i in range(-24, 25):
                    t = now + timedelta(hours=i)
                    hour_val = t.hour + t.minute / 60.0
                    pred = generate_diurnal_power(hour_val, peak_kw)
                    noise = math.sin(i * 1.5 + idx) * peak_kw * 0.018
                    actual = (
                        round(pred * 0.96 + noise, 2)
                        if i <= 0 and pred > 0
                        else (0.0 if i <= 0 else None)
                    )
                    fc_batch.append(ForecastRecord(
                        plant_id=plant.id,
                        forecast_time=t,
                        generated_at=now - timedelta(hours=24),
                        predicted_power_kw=pred,
                        actual_power_kw=actual,
                        confidence_lower=round(max(0.0, pred * 0.88), 2),
                        confidence_upper=round(pred * 1.08, 2),
                        model_name="SolarPulse-Hybrid-Ensemble",
                        model_version="2.1.0",
                        physics_power_kw=round(pred * 0.98, 2),
                        ml_power_kw=pred,
                    ))
                session.add_all(fc_batch)
                logger.info(f"    → {len(fc_batch)} forecast records")

            # ── SCADA telemetry (last 3 hours, every 10 min) ─────────────────
            existing_scada = await session.scalar(
                select(ScadaReading.id).where(ScadaReading.plant_id == plant.id)
            )
            if not existing_scada:
                base_temp = pd["base_temp_c"]
                peak_kw = pd["peak_gen_kw"]
                scada_batch = []
                for m in range(0, 180, 10):
                    t = now - timedelta(minutes=m)
                    h = t.hour + t.minute / 60.0
                    gen = generate_diurnal_power(h, peak_kw)
                    irr = round(gen / (peak_kw / 850.0) + 15.0, 1) if gen > 0 else 0.0
                    scada_batch.append(ScadaReading(
                        plant_id=plant.id,
                        timestamp=t,
                        power_kw=gen,
                        irradiance_w_m2=irr,
                        temperature_c=base_temp + (2.0 if gen > 0 else -4.0),
                        wind_speed_m_s=3.2 + idx * 0.3,
                        module_temperature_c=base_temp + 12.0 if gen > 0 else base_temp - 6.0,
                        inverter_power_kw=round(gen * 0.985, 2),
                        expected_power_kw=gen,
                    ))
                session.add_all(scada_batch)
                logger.info(f"    → {len(scada_batch)} SCADA readings")

            # ── Alerts ────────────────────────────────────────────────────────
            existing_alerts = await session.scalar(
                select(AnomalyAlert.id).where(AnomalyAlert.plant_id == plant.id)
            )
            if not existing_alerts:
                alert_batch = []
                for ad in pd["alerts"]:
                    alert_batch.append(AnomalyAlert(
                        plant_id=plant.id,
                        timestamp=now - timedelta(hours=ad["hours_ago"]),
                        alert_type=ad["alert_type"],
                        severity=ad["severity"],
                        message=ad["message"],
                        expected_power_kw=ad["expected"],
                        actual_power_kw=ad["actual"],
                        deviation_percent=ad["deviation"],
                        is_resolved=ad["resolved"],
                        resolved_at=(now - timedelta(hours=ad["hours_ago"] - 1)
                                     if ad["resolved"] else None),
                    ))
                session.add_all(alert_batch)
                logger.info(f"    → {len(alert_batch)} alerts")

        await session.commit()
        logger.info("✅ Seeding complete — 3 Indian solar plants ready.")


if __name__ == "__main__":
    asyncio.run(seed())
