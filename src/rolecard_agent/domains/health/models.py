"""DTOs for the health domain: MedicalReport, MedicalIndex, and their create/update forms.

There is deliberately NO user/tenant DTO here. Those are kernel concepts and live in
core/; this domain only references `app_user.user_id` as a foreign key
(A2, C6).

MedicalIndex carries provenance so every value can be traced back to a source:
is_verified / source(manual|parsed|ocr) / raw_text / verified_at.
"""
