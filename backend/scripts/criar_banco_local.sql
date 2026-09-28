-- Cria o usuário e o banco do CRM Assist em um PostgreSQL local (sem Docker).
-- Os valores batem com o POSTGRES_URL padrão do .env.example.
--
-- Uso (pede a senha do superusuário "postgres"):
--   psql -U postgres -h localhost -f scripts/criar_banco_local.sql

SELECT 'CREATE ROLE crm LOGIN PASSWORD ''crm'''
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'crm')
\gexec

SELECT 'CREATE DATABASE crmassistant OWNER crm'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'crmassistant')
\gexec
