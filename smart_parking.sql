-- =====================================================================
--  SMART PARKING BARRIER - Skema database MySQL / MariaDB
--  Cara import:
--      mysql -u root -p < smart_parking.sql
--  atau lewat phpMyAdmin / MySQL Workbench: Import -> pilih file ini.
--
--  Tabel:
--    cards            data kartu RFID + plat nomor terakhir + status di dalam/luar
--    vehicle_images   gambar kendaraan (disimpan langsung sebagai BLOB)
--    access_logs      riwayat akses (izin, ditolak, alert, override, dll.)
--    parking_records  catatan kendaraan masuk dan keluar (1 baris = 1 kunjungan)
--    settings         kapasitas slot & jumlah tamu (override)
-- =====================================================================

CREATE DATABASE IF NOT EXISTS `smart_parking`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `smart_parking`;

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS cards (
        uid         VARCHAR(32)  NOT NULL,
        name        VARCHAR(100) NOT NULL DEFAULT '',
        plate       VARCHAR(20)  NOT NULL DEFAULT '',
        active      TINYINT(1)   NOT NULL DEFAULT 1,
        `inside`    TINYINT(1)   NOT NULL DEFAULT 0,
        last_seen   DATETIME     NULL,
        created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (uid),
        KEY idx_cards_plate (plate)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS vehicle_images (
        id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
        filename    VARCHAR(120) NOT NULL,
        uid         VARCHAR(32)  NULL,
        event       VARCHAR(30)  NOT NULL DEFAULT '',
        plate       VARCHAR(20)  NOT NULL DEFAULT '',
        mime_type   VARCHAR(30)  NOT NULL DEFAULT 'image/jpeg',
        size_bytes  INT UNSIGNED NOT NULL DEFAULT 0,
        image       LONGBLOB     NOT NULL,
        captured_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        UNIQUE KEY uq_image_filename (filename),
        KEY idx_image_uid (uid),
        KEY idx_image_plate (plate),
        KEY idx_image_time (captured_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS access_logs (
        id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        ts      DATETIME     NOT NULL,
        device  VARCHAR(40)  NOT NULL DEFAULT '',
        event   VARCHAR(30)  NOT NULL,
        uid     VARCHAR(32)  NOT NULL DEFAULT '',
        plate   VARCHAR(20)  NOT NULL DEFAULT '',
        detail  TEXT         NULL,
        photo   VARCHAR(120) NULL,
        PRIMARY KEY (id),
        KEY idx_logs_ts (ts),
        KEY idx_logs_event (event),
        KEY idx_logs_uid (uid),
        KEY idx_logs_plate (plate),
        KEY idx_logs_photo (photo),
        CONSTRAINT fk_logs_photo FOREIGN KEY (photo) REFERENCES vehicle_images (filename)
            ON DELETE SET NULL ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS parking_records (
        id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        uid           VARCHAR(32)  NULL,
        card_name     VARCHAR(100) NOT NULL DEFAULT '',
        plate         VARCHAR(20)  NOT NULL DEFAULT '',
        is_guest      TINYINT(1)   NOT NULL DEFAULT 0,
        entry_time    DATETIME     NULL,
        entry_device  VARCHAR(40)  NULL,
        entry_photo   VARCHAR(120) NULL,
        exit_time     DATETIME     NULL,
        exit_device   VARCHAR(40)  NULL,
        exit_photo    VARCHAR(120) NULL,
        status        ENUM('DI DALAM','SELESAI','DIRESET') NOT NULL DEFAULT 'DI DALAM',
        PRIMARY KEY (id),
        KEY idx_rec_uid (uid),
        KEY idx_rec_plate (plate),
        KEY idx_rec_status (status),
        KEY idx_rec_entry (entry_time),
        KEY idx_rec_exit (exit_time),
        CONSTRAINT fk_rec_entry_photo FOREIGN KEY (entry_photo) REFERENCES vehicle_images (filename)
            ON DELETE SET NULL ON UPDATE CASCADE,
        CONSTRAINT fk_rec_exit_photo FOREIGN KEY (exit_photo) REFERENCES vehicle_images (filename)
            ON DELETE SET NULL ON UPDATE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS settings (
        setting_key   VARCHAR(50)  NOT NULL,
        setting_value VARCHAR(255) NOT NULL DEFAULT '',
        PRIMARY KEY (setting_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO settings (setting_key, setting_value) VALUES ('max_slots', '50');
INSERT IGNORE INTO settings (setting_key, setting_value) VALUES ('guests', '0');
