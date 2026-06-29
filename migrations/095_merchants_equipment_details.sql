-- Migration 095 — merchants.equipment_details JSONB column. Holds the
-- most-recent equipment-invoice / quote extraction for merchants on the
-- equipment product. One JSONB blob, mirrors the EquipmentInvoiceResult
-- shape (description, make, model, year, condition, serial_number, vin,
-- vendor_name, total_cost as Decimal-safe string).

ALTER TABLE merchants
  ADD COLUMN IF NOT EXISTS equipment_details jsonb;

COMMENT ON COLUMN merchants.equipment_details IS
  'Equipment invoice / quote extraction (parser/equipment/). Updated '
  'on each upload — see audit_log.equipment.extraction_recorded.';
