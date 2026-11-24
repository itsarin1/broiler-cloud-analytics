"""Cloud Analytics Kandang Ayam, berbasis Blynk V0-V25.
Hanya gunakan pembacaan valid. Tidak mengarang ppm NH3, ADG/FCR/IP, atau target pakan.
Dijalankan setiap 5 menit via GitHub Actions.
"""
import csv
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TZ_WIB = timezone(timedelta(hours=7))
NOW = datetime.now(TZ_WIB)
TODAY = NOW.date().isoformat()
TOKEN = os.environ["BLYNK_TOKEN"]
BASE_URL = (os.environ.get("BLYNK_BASE_URL") or "https://blynk.cloud").rstrip("/")
TARGET_PER_BIRD_G = float(os.environ.get("FEED_TARGET_G_PER_BIRD", "0"))
# Ini harus didasarkan pada tabel kebutuhan pakan per umur/strain, BUKAN nilai tetap untuk produksi.
START_WEIGHT_G = float(os.environ.get("START_WEIGHT_G", "0"))
TRACKING_FROM_DAY_1 = os.environ.get("TRACKING_FROM_DAY_1", "false").lower() == "true"
DATA = Path("data")
DATA.mkdir(exist_ok=True)
STATE = DATA / "state_analytics.json"
LOG = DATA / "log_5menit_v2.csv"
DAILY = DATA / "rekap_harian_v2.csv"

http = requests.Session()


def api(path, params):
    r = http.get(f"{BASE_URL}/external/api/{path}", params={"token": TOKEN, **params}, timeout=15)
    r.raise_for_status()
    return r


def device_online():
    txt = api("isHardwareConnected", {}).text.strip().lower()
    return txt == "true"


def get_pin(n):
    # Blynk mengembalikan string atau array JSON, tergantung endpoint/versi.
    try:
        r = api("get", {f"v{n}": ""})
        try:
            value = r.json()
        except ValueError:
            value = r.text.strip()
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            return None
        v = float(value)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError, requests.RequestException) as exc:
        print(f"V{n}: tidak tersedia ({exc})")
        return None


def put_pin(n, value):
    if value is None or not math.isfinite(float(value)):
        return
    api("update", {f"v{n}": f"{value:.3f}"})
    print(f"Update V{n} = {value:.3f}")


def put_string(n, value):
    api("update", {f"v{n}": value})


def event(code, description):
    api("logEvent", {"code": code, "description": description[:480]})
    print("EVENT", code)


def valid(v, lo, hi):
    return v is not None and lo <= v <= hi


def append_csv(file, header, row):
    exists = file.exists() and file.stat().st_size > 0
    with file.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(header)
        w.writerow(row)


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"date": TODAY, "last_feed_g": None, "consumed_g": 0.0,
            "last_weight_g": None, "yesterday_end_weight_g": None,
            "total_feed_g": 0.0, "first_weight_g": None,
            "last_heat_alert": None, "last_low_feed_alert_date": None}


def save_state(s):
    STATE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")


def predict_30min():
    if not LOG.exists():
        return None
    rows = []
    with LOG.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                d = datetime.fromisoformat(r["timestamp"])
                v = float(r["suhu_c"])
                if math.isfinite(v): rows.append((d, v))
            except (ValueError, KeyError):
                pass
    cutoff = NOW - timedelta(minutes=90)
    rows = [(d, v) for d, v in rows if d >= cutoff][-18:]
    if len(rows) < 3 or (rows[-1][0]-rows[0][0]).total_seconds() < 600:
        return None
    t0 = rows[0][0]
    xs = [(d-t0).total_seconds()/60 for d, _ in rows]
    ys = [v for _, v in rows]
    mx, my = sum(xs)/len(xs), sum(ys)/len(ys)
    den = sum((x-mx)**2 for x in xs)
    if den == 0: return None
    slope = sum((x-mx)*(y-my) for x,y in zip(xs,ys))/den
    pred = my + slope*(xs[-1]+30-mx)
    return round(pred, 2) if 0 <= pred <= 60 else None


def main():
    if not device_online():
        print("ESP32 OFFLINE: data tidak dihitung, tidak membuat baris 0.")
        return
    s = load_state()
    temp, rh = get_pin(0), get_pin(1)
    raw_gas, chicken, feed = get_pin(2), get_pin(3), get_pin(4)
    umur, populasi, mati = get_pin(22), get_pin(23), get_pin(12)
    if not valid(temp, 5, 55) or not valid(rh, 1, 100):
        print("Data DHT belum valid; analytics dibatalkan, hindari data palsu.")
        return

    # Rekap saat tanggal berganti; perhitungan kemarin hanya jika dataset cukup.
    if s["date"] != TODAY:
        previous_end = s.get("yesterday_end_weight_g")
        last_weight = s.get("last_weight_g")
        consumption = float(s.get("consumed_g", 0))
        adg = None
        if valid(previous_end, 1, 10000) and valid(last_weight, 1, 10000):
            gain = last_weight - previous_end
            if gain > 0: adg = gain
        flock = max(1, int(populasi or 1) - int(mati or 0))
        fcr_day = consumption / (adg * flock) if adg and consumption > 0 else None
        if fcr_day is not None and 0 < fcr_day <= 10: put_pin(14, fcr_day)
        if adg is not None: put_pin(13, adg)
        append_csv(DAILY,
                   ["tanggal", "konsumsi_g", "bobot_akhir_g", "adg_g_per_hari", "fcr_harian"],
                   [s["date"], round(consumption,2), last_weight, adg, fcr_day])
        if TARGET_PER_BIRD_G > 0 and valid(populasi,1,10000):
            day_target = TARGET_PER_BIRD_G * flock
            if consumption/day_target < 0.8 and s.get("last_low_feed_alert_date") != s["date"]:
                event("konsumsi_pakan_rendah",
                      f"Konsumsi {consumption:.0f} g, target {day_target:.0f} g. Evaluasi kondisi kandang dan pakan.")
                s["last_low_feed_alert_date"] = s["date"]
        s["yesterday_end_weight_g"] = last_weight
        s["date"] = TODAY
        s["consumed_g"] = 0.0
        s["last_feed_g"] = None

    # Pakan: selisih stok antarpengukuran. Sensitif terhadap pakan tercecer dan noise load cell.
    if valid(feed,0,10000):
        prev = s.get("last_feed_g")
        if prev is not None:
            delta = prev - feed
            if 3.0 <= delta <= 3000.0:
                s["consumed_g"] += delta
                s["total_feed_g"] += delta
            elif delta < -3:
                print(f"Terindikasi pengisian ulang pakan: {-delta:.1f} g")
        s["last_feed_g"] = feed
        put_pin(11, s["consumed_g"])
    if valid(chicken,1,10000):
        s["last_weight_g"] = chicken
        if s.get("first_weight_g") is None:
            s["first_weight_g"] = chicken
        put_pin(10, chicken)  # satu timbangan ayam, bukan rerata populasi

    # THI memakai rumus awal proyek; ambang biologis harus divalidasi khusus strain/umur.
    thi = round(0.8 * temp + (rh/100.0)*(temp-14.4)+46.4,2)
    put_pin(16,thi)
    append_csv(LOG,["timestamp","suhu_c","rh_persen","gas_adc_0db","bobot_ayam_g","stok_pakan_g","thi"],
               [NOW.isoformat(),temp,rh,raw_gas,chicken,feed,thi])
    forecast = predict_30min()
    if forecast is not None:
        put_pin(17,forecast)
        if forecast >= 30:
            prev = s.get("last_heat_alert")
            allow = prev is None or (NOW-datetime.fromisoformat(prev)).total_seconds() >= 3600
            if allow:
                event("peringatan_risiko_cekaman_panas",
                      f"Prediksi suhu 30 menit ke depan {forecast:.1f}°C. Periksa ventilasi dan kondisi ayam.")
                s["last_heat_alert"] = NOW.isoformat()
        put_string(19,f"FAN:{29.0 if forecast>=30 else 30.0:.1f},CONV:6")

    # Target pakan hanya aktif jika tabel kebutuhan/umur ditentukan pengguna.
    flock = max(1,int(populasi or 1)-int(mati or 0))
    if TARGET_PER_BIRD_G > 0 and valid(populasi,1,10000):
        target = TARGET_PER_BIRD_G * flock
        put_pin(21,target)
        put_pin(24,100.0*s["consumed_g"]/target)
        put_pin(25,target-s["consumed_g"])

    # FCR kumulatif/IP hanya jika dicatat sejak hari pertama dan berat awal valid.
    first = s.get("first_weight_g")
    if TRACKING_FROM_DAY_1 and START_WEIGHT_G > 0 and valid(chicken,1,10000) \
            and chicken > START_WEIGHT_G and s["total_feed_g"] > 0 and valid(umur,1,60) \
            and valid(populasi,1,10000):
        gain = (chicken-START_WEIGHT_G)*flock
        fcr = s["total_feed_g"]/gain
        if 0 < fcr <= 10:
            put_pin(14,fcr)
            sr = 100*flock/populasi
            ip = sr*(chicken/1000)*100/(umur*fcr)
            if 0 <= ip <= 1000: put_pin(15,ip)

    save_state(s)
    print("Selesai:", NOW.isoformat(), "suhu",temp,"RH",rh,"stok",feed,"konsumsi",s["consumed_g"])


if __name__ == "__main__":
    main()
