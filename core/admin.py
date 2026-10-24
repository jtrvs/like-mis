from django.contrib import admin

from . import models

for m in (models.Profile, models.DoctorProfile, models.PatientProfile, models.CareLink, models.Consent,
          models.TrustedContact, models.Diagnosis, models.Condition, models.Prescription, models.MedicationIntake,
          models.ScaleResponse, models.DailyCheckin, models.Message, models.Referral, models.DoctorDecision, models.AuditLog):
    admin.site.register(m)
