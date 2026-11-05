BEGIN TRANSACTION;
CREATE TABLE albums (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '',
                cover TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL, dirty INTEGER NOT NULL DEFAULT 1, filter TEXT NOT NULL DEFAULT '', auto_people TEXT NOT NULL DEFAULT '[]', entity TEXT NOT NULL DEFAULT '', record TEXT NOT NULL DEFAULT '{}');
INSERT INTO "albums" VALUES('c49255fdfc524508a0b1f0695ed7afd2ceeafe3d95d16eb6a015f2ad935b9fb4','Fixture album','Synthetic preserved description','485c40002d6ba2251492d696e1960179ab7cf0de9b856d88093573c2424c1d8a',1,0,0,1700000000.125,1700000000.125,1,'tag:fixture','["37471b356aba3c12a3479285ca89e098"]','0123456789abcdef0123456789abcdef','{"unknown":{"keep":[null,"旅行"]}}');
INSERT INTO "albums" VALUES('83d6ecbb59b72e391b681791b33ccf26434f5e2579991c5a253bf6b229bb0dfc','Imported only','','',0,0,0,1700000000.125,1700000000.125,1,'','[]','','{}');
CREATE TABLE face_scans (
                photo_id TEXT PRIMARY KEY, model TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
INSERT INTO "face_scans" VALUES('485c40002d6ba2251492d696e1960179ab7cf0de9b856d88093573c2424c1d8a','synthetic-face','');
CREATE TABLE faces (
                id TEXT PRIMARY KEY, photo_id TEXT NOT NULL, person_id TEXT NOT NULL,
                rect TEXT NOT NULL, embedding BLOB NOT NULL,
                excluded TEXT NOT NULL DEFAULT '[]');
INSERT INTO "faces" VALUES('synthetic-face','485c40002d6ba2251492d696e1960179ab7cf0de9b856d88093573c2424c1d8a','37471b356aba3c12a3479285ca89e098','[0.1,0.2,0.3,0.4]',X'0000803F0000003F000080BF00000000','["synthetic-excluded-person"]');
CREATE TABLE motion_scans (
                photo_id TEXT PRIMARY KEY, reader_version INTEGER NOT NULL);
INSERT INTO "motion_scans" VALUES('485c40002d6ba2251492d696e1960179ab7cf0de9b856d88093573c2424c1d8a',1);
CREATE TABLE people (
                id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                ignored INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
                birth TEXT NOT NULL DEFAULT '');
INSERT INTO "people" VALUES('37471b356aba3c12a3479285ca89e098','Synthetic person',0,1700000000.125,'1962-02-03');
CREATE TABLE person_tag_edits (photo_id TEXT PRIMARY KEY);
INSERT INTO "person_tag_edits" VALUES('485c40002d6ba2251492d696e1960179ab7cf0de9b856d88093573c2424c1d8a');
CREATE TABLE person_tags (
                photo_id TEXT NOT NULL, person_id TEXT NOT NULL,
                PRIMARY KEY(photo_id,person_id));
INSERT INTO "person_tags" VALUES('485c40002d6ba2251492d696e1960179ab7cf0de9b856d88093573c2424c1d8a','37471b356aba3c12a3479285ca89e098');
CREATE TABLE photos (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, extension TEXT NOT NULL,
                kind TEXT NOT NULL, size INTEGER NOT NULL, taken REAL NOT NULL,
                width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL, albums TEXT NOT NULL,
                favorite INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                trashed INTEGER NOT NULL DEFAULT 0, caption TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
                dirty INTEGER NOT NULL DEFAULT 1, manifest TEXT NOT NULL DEFAULT '{}', edits TEXT NOT NULL DEFAULT '{}');
INSERT INTO "photos" VALUES('485c40002d6ba2251492d696e1960179ab7cf0de9b856d88093573c2424c1d8a','Synthetic ''trip'' 旅行.jpg','.jpg','photo',128,-154641294.5,17,9,'{"camera_date":"1965:02:03 04:05:06","unknown":{"label":"Synthetic: sí 旅行","large_integer":1152921504606846979,"list":[true,2],"null":null}}','["Fixture album","Imported only"]',1,0,1,0,'Synthetic caption with ''quotes'' and sí','fixture,旅行',1700000000.125,1,'{"unknown":{"preserve":[null,1152921504606846983]}}','{"rotate":90,"unknown":{"keep":true}}');
CREATE TABLE semantic_embeddings (
                photo_id TEXT PRIMARY KEY, model TEXT NOT NULL,
                embedding BLOB, error TEXT NOT NULL DEFAULT '');
INSERT INTO "semantic_embeddings" VALUES('485c40002d6ba2251492d696e1960179ab7cf0de9b856d88093573c2424c1d8a','synthetic-clip',X'0000803F0000003F000080BF00000000','');
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "settings" VALUES('album_catalog_migrated','true');
INSERT INTO "settings" VALUES('fixture_unknown','{"enabled":false,"large_integer":1152921504606846987,"nested":{"null":null},"unicode":"旅行"}');
CREATE TABLE sources (
                path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER,
                meta_stamp TEXT, photo_id TEXT REFERENCES photos(id));
INSERT INTO "sources" VALUES('/synthetic/photos/trip.jpg',128,1700000000000000000,'synthetic-stamp','485c40002d6ba2251492d696e1960179ab7cf0de9b856d88093573c2424c1d8a');
CREATE INDEX photo_dates ON photos(taken DESC, id);
CREATE INDEX faces_person ON faces(person_id,photo_id);
CREATE INDEX faces_photo ON faces(photo_id);
CREATE INDEX person_tags_person ON person_tags(person_id,photo_id);
CREATE VIEW person_photos AS
                SELECT photo_id,person_id FROM faces UNION SELECT photo_id,person_id FROM person_tags;
COMMIT;
