import os
import csv
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from sklearn.linear_model import LinearRegression

# Mengambil token dari GitHub Secrets
TOKEN = os.environ.get("BLYNK_TOKEN")
BASE_URL = "https://blynk.cloud/external/api"

# Konfigurasi awal kandang (sesuaikan dengan kondisi uji)
POPULASI_AWAL = 100  # ekor (contoh skala simulasi/kandang)
UMUR_AYAM_HARI = 21  # fase finisher (hari ke-15 s.d. panen)

def get_blynk_pin(pin):
    """Membaca nilai terbaru dari datastream Blynk"""
    try:
        res = requests.get(f"{BASE_URL}/get?token={TOKEN}&{pin}", timeout=10)
        if res.status_code == 200 and res.text != "":
            return float(res.text)
    except Exception as e:
        print(f"Gagal membaca {pin}: {e}")
    return 0.0

def update_blynk_pin(pin, value):
    """Mengirim hasil perhitungan ke datastream Blynk"""
    try:
        res = requests.get(f"{BASE_URL}/update?token={TOKEN}&{pin}={value}", timeout=10)
        print(f"Update {pin} -> {value} (Status: {res.status_code})")
    except Exception as e:
        print(f"Gagal update {pin}: {e}")

def main():
    os.makedirs("data", exist_ok=True)
    wib = timezone(timedelta(hours=7))
    now = datetime.now(wib)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    tanggal_str = now.strftime("%Y-%m-%d")

    # 1. Ambil data terbaru dari Blynk
    suhu = get_blynk_pin("V1")
    rh = get_blynk_pin("V2")
    amonia = get_blynk_pin("V3")
    bobot_g = get_blynk_pin("V8")
    pakan_g = get_blynk_pin("V9")
    mortalitas = int(get_blynk_pin("V14"))

    # 2. Hitung THI (Temperature-Humidity Index)
    thi = round(0.8 * suhu + (rh / 100.0) * (suhu - 14.4) + 46.4, 2)

    # 3. Simpan ke log sementara (per 5 menit) untuk bahan Regresi Linear
    log_file = "data/log_5menit.csv"
    log_exists = os.path.isfile(log_file)
    with open(log_file, "a", newline="") as f:
        writer = csv.writer(f)
        if not log_exists:
            writer.writerow(["timestamp", "suhu", "rh", "amonia", "thi", "bobot_g"])
        writer.writerow([timestamp_str, suhu, rh, amonia, thi, bobot_g])

    # 4. Prediksi Suhu 30 Menit ke Depan (Regresi Linear dari 12 data terakhir = 1 jam)
    df_log = pd.read_csv(log_file)
    if len(df_log) >= 3:
        df_recent = df_log.tail(12).reset_index(drop=True)
        X = np.arange(len(df_recent)).reshape(-1, 1)
        y = df_recent["suhu"].values
        model = LinearRegression().fit(X, y)
        # Prediksi 6 langkah ke depan (6 x 5 menit = 30 menit)
        step_30m = np.array([[len(df_recent) + 5]])
        prediksi_suhu = round(float(model.predict(step_30m)[0]), 2)
    else:
        # Jika data historis belum cukup (baru pertama kali jalan), gunakan suhu saat ini
        prediksi_suhu = suhu

    update_blynk_pin("V12", prediksi_suhu)

    # 5. Rekap Harian (ADG, FCR, IP) & Pengarsipan CSV
    arsip_file = "data/arsip_harian.csv"
    arsip_exists = os.path.isfile(arsip_file)

    # Cek bobot kemarin dari baris terakhir arsip_harian.csv
    bobot_kemarin = bobot_g - 50.0  # nilai default jika hari pertama
    if arsip_exists:
        df_arsip = pd.read_csv(arsip_file)
        if len(df_arsip) > 0:
            bobot_kemarin = float(df_arsip.iloc[-1]["Bobot_g"])

    # Hitung metrik performa
    adg = round(max(bobot_g - bobot_kemarin, 0.0), 2)
    fcr = round(pakan_g / adg, 2) if adg > 0 else 0.0
    ayam_hidup = max(POPULASI_AWAL - mortalitas, 1)
    daya_hidup_persen = (ayam_hidup / POPULASI_AWAL) * 100.0
    bobot_kg = bobot_g / 1000.0
    ip = round(((daya_hidup_persen * bobot_kg) / (fcr * UMUR_AYAM_HARI)) * 100, 2) if fcr > 0 else 0.0

    # Jalankan update harian setiap jam 23:50 - 23:59 WIB (atau saat file arsip belum ada)
    if (now.hour == 23 and now.minute >= 50) or not arsip_exists:
        update_blynk_pin("V10", adg)
        update_blynk_pin("V11", fcr)
        update_blynk_pin("V13", ip)

        # Umpan balik parameter adaptif ke ESP32 via V15
        ambang_fan = 29.0 if prediksi_suhu > 30.0 else 30.0
        update_blynk_pin("V15", f"FAN:{ambang_fan},CONV:6")

        with open(arsip_file, "a", newline="") as f:
            writer = csv.writer(f)
            if not arsip_exists:
                writer.writerow(["Tanggal", "Suhu", "RH", "Amonia", "THI", "Bobot_g", "ADG", "FCR", "IP"])
            writer.writerow([tanggal_str, suhu, rh, amonia, thi, bobot_g, adg, fcr, ip])

    # Batasi ukuran log_5menit.csv agar tidak membengkak (simpan 288 baris = 24 jam terakhir)
    if len(df_log) > 288:
        df_log.tail(288).to_csv(log_file, index=False)

if __name__ == "__main__":
    main()
