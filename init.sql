CREATE DATABASE IF NOT EXISTS npm_stats CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE npm_stats;

CREATE TABLE IF NOT EXISTS access_stats (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    domain VARCHAR(255) NOT NULL,
    client_ip VARCHAR(50) NOT NULL,
    access_date DATE NOT NULL,
    count INT NOT NULL DEFAULT 0,
    UNIQUE KEY uniq_access (domain, client_ip, access_date),
    INDEX idx_domain (domain),
    INDEX idx_date (access_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS sync_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(20) NOT NULL,
    message TEXT
) ENGINE=InnoDB;
