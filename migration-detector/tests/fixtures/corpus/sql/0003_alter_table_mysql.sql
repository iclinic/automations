ALTER TABLE ledger_entry_v2 ADD PRIMARY KEY (id);
ALTER TABLE ledger_entry_v2 ADD FOREIGN KEY (owner_id) REFERENCES ledger_entry (id);
ALTER TABLE ledger_entry_v2 ADD INDEX ledger_entry_title_idx (title);
ALTER TABLE ledger_entry_v2 ADD UNIQUE INDEX ledger_entry_title_uidx (title);
ALTER TABLE ledger_entry_v2 DROP KEY ledger_entry_title_idx;
ALTER TABLE ledger_entry_v2 MODIFY title varchar(200) NULL;
ALTER TABLE ledger_entry_v2 MODIFY owner_id bigint NOT NULL;
ALTER TABLE ledger_entry_v2 CHANGE title heading varchar(200) NULL;
