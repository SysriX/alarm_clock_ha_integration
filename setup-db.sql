-- Create the MariaDB/MySQL database and user for the "wecker" integration (run once as root,
-- e.g. phpMyAdmin -> SQL, or: mariadb -u root -p < setup-db.sql).
-- Replace CHANGE_ME with your own password and use the same one in secrets.yaml.
CREATE DATABASE IF NOT EXISTS wecker CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'wecker'@'%' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL PRIVILEGES ON wecker.* TO 'wecker'@'%';
FLUSH PRIVILEGES;
