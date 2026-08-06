-- HxSync field mappings can name a WASM module as their transform (#17).
--
-- `transform` stays a short enum ('passthrough', 'to_int', 'to_string',
-- 'seconds_to_hours', and now 'wasm'); the module is named separately rather
-- than by widening that column and packing two facts into one string.
--
-- NULL for every existing mapping, and meaningful only when transform='wasm'.

ALTER TABLE sync_field_mappings
    ADD COLUMN IF NOT EXISTS transform_module_id VARCHAR(255);
