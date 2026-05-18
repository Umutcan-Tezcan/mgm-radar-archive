"""
MGM Radar Image Fetcher & Google Drive Uploader
================================================
Her calistiginda MGM'nin guncel radar goruntusunu ceker ve
beligrlenen Google Drive klasorune yukler.

Ortam Degiskenleri (GitHub Secrets):
  DRIVE_TOKEN        : OAuth2 token.json dosyasinin tam icerigi
  DRIVE_FOLDER_ID    : Yuklenecek Drive klasorunun ID'si
  RADAR_STATION      : (Opsiyonel) Istasyon kodu, varsayilan: ist
  RADAR_PRODUCT      : (Opsiyonel) Urun kodu, varsayilan: ppi
"""

import os
import sys
import json
import logging
import tempfile
from datetime import datetime, timezone, timedelta

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ---------------------------------------------------------------------------
# Loglama
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------
# MGM radar görüntüsü URL deseni:
# https://mgm.gov.tr/FTPDATA/uzal/radar/{il}/{il}{urun}{frame}.jpg
# frame=15 her zaman en güncel taramayı içerir.
MGM_RADAR_URL_TEMPLATE = (
    "https://mgm.gov.tr/FTPDATA/uzal/radar/{station}/{station}{product}15.jpg"
)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# MGM sunucusunun bot engellemesini atlatmak için gerçekçi bir User-Agent
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.mgm.gov.tr/sondurum/radar.aspx",
    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
}

REQUEST_TIMEOUT = 30  # saniye


# ---------------------------------------------------------------------------
# Yardımcı fonksiyonlar
# ---------------------------------------------------------------------------

def get_env(key: str, default: str | None = None, required: bool = True) -> str:
    """Ortam değişkenini okur; zorunluysa ve yoksa çıkış yapar."""
    value = os.environ.get(key, default)
    if required and not value:
        log.error("Zorunlu ortam değişkeni bulunamadı: %s", key)
        sys.exit(1)
    return value  # type: ignore[return-value]


def build_drive_service(token_json_str: str):
    """OAuth2 token JSON stringinden Drive API istemcisi olusturur."""
    try:
        token_data = json.loads(token_json_str)
    except json.JSONDecodeError as exc:
        log.error("DRIVE_TOKEN gecerli bir JSON degil: %s", exc)
        sys.exit(1)

    credentials = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data.get("scopes", DRIVE_SCOPES),
    )
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    log.info("Google Drive API baglantisi kuruldu (OAuth2).")
    return service


def fetch_radar_image(station: str, product: str) -> bytes:
    """MGM sunucusundan radar görüntüsünü indirir ve baytlarını döndürür."""
    url = MGM_RADAR_URL_TEMPLATE.format(station=station, product=product)
    log.info("Radar görüntüsü indiriliyor: %s", url)

    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        log.error("HTTP hatası: %s", exc)
        raise
    except requests.exceptions.ConnectionError as exc:
        log.error("Bağlantı hatası: %s", exc)
        raise
    except requests.exceptions.Timeout:
        log.error("İstek zaman aşımına uğradı (%s sn).", REQUEST_TIMEOUT)
        raise

    content_type = response.headers.get("Content-Type", "")
    if "image" not in content_type:
        log.error(
            "Beklenmeyen içerik türü: %s — sunucu muhtemelen bot engeli uyguladı.",
            content_type,
        )
        raise ValueError(f"Beklenen image/*, alınan: {content_type}")

    log.info(
        "Görüntü başarıyla indirildi. Boyut: %.1f KB", len(response.content) / 1024
    )
    return response.content


def save_temp_image(data: bytes, filename: str) -> str:
    """Görüntü baytlarını geçici bir dosyaya kaydeder ve yolunu döndürür."""
    tmp_dir = tempfile.gettempdir()
    filepath = os.path.join(tmp_dir, filename)
    with open(filepath, "wb") as f:
        f.write(data)
    log.info("Geçici dosya oluşturuldu: %s", filepath)
    return filepath


def upload_to_drive(service, filepath: str, filename: str, folder_id: str) -> str:
    """
    Dosyayı Google Drive klasörüne yükler.
    Aynı isimde dosya varsa üzerine yazar (günceller), yoksa yeni oluşturur.
    Yüklenen dosyanın Drive ID'sini döndürür.
    """
    # Aynı isimde mevcut dosya var mı kontrol et
    query = (
        f"name='{filename}' and "
        f"'{folder_id}' in parents and "
        "trashed=false"
    )
    results = (
        service.files()
        .list(q=query, fields="files(id, name)", spaces="drive")
        .execute()
    )
    existing_files = results.get("files", [])

    media = MediaFileUpload(filepath, mimetype="image/jpeg", resumable=False)

    if existing_files:
        # Mevcut dosyayı güncelle
        file_id = existing_files[0]["id"]
        log.info("Mevcut dosya güncelleniyor (ID: %s)…", file_id)
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        # Yeni dosya oluştur
        file_metadata = {"name": filename, "parents": [folder_id]}
        log.info("Yeni dosya oluşturuluyor: %s", filename)
        result = (
            service.files()
            .create(body=file_metadata, media_body=media, fields="id")
            .execute()
        )
        file_id = result.get("id")

    log.info("Drive yüklemesi tamamlandı. Dosya ID: %s", file_id)
    return file_id


# ---------------------------------------------------------------------------
# Ana akış
# ---------------------------------------------------------------------------

def main() -> None:
    # -- Ortam değişkenleri --------------------------------------------------
    drive_token = get_env("DRIVE_TOKEN")
    folder_id   = get_env("DRIVE_FOLDER_ID")
    station     = get_env("RADAR_STATION", default="ist", required=False)
    product     = get_env("RADAR_PRODUCT", default="ppi", required=False)

    # -- Zaman damgalı dosya adı ---------------------------------------------
    now_utc  = datetime.now(timezone.utc)
    filename = now_utc.strftime("%Y-%m-%d_%H-%M") + f"_{station}_{product}_radar.jpg"
    log.info("Oturum başladı | %s", now_utc.isoformat())

    # -- Radar görüntüsü indir ------------------------------------------------
    image_data = fetch_radar_image(station, product)

    # -- Geçici dosyaya kaydet ------------------------------------------------
    tmp_path = save_temp_image(image_data, filename)

    # -- Google Drive'a yükle -------------------------------------------------
    try:
        drive_service = build_drive_service(drive_token)
        upload_to_drive(drive_service, tmp_path, filename, folder_id)
    finally:
        # Her koşulda geçici dosyayı temizle
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
            log.info("Geçici dosya silindi: %s", tmp_path)

    log.info("Tüm işlemler başarıyla tamamlandı.")


if __name__ == "__main__":
    main()
