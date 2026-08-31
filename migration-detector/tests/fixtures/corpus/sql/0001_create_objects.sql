BEGIN;
SET lock_timeout = '5s';
CREATE SEQUENCE ledger_entry_id_seq;
CREATE TYPE ledger_kind AS ENUM ('debit', 'credit');
CREATE TABLE IF NOT EXISTS ledger_entry (
    id bigint NOT NULL DEFAULT nextval('ledger_entry_id_seq'),
    kind ledger_kind NOT NULL,
    label varchar(120) NULL
);
CREATE INDEX ledger_entry_kind_idx ON ledger_entry (kind);
CREATE UNIQUE INDEX ledger_entry_id_uidx ON ledger_entry (id);
COMMENT ON TABLE ledger_entry IS 'lancamentos';
COMMIT;
