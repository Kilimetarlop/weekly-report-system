# SPDX-License-Identifier: PolyForm-Strict-1.0.0
# Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed.
from django.db import migrations

SQL = r'''
CREATE TABLE reports_throttle (key varchar(64) PRIMARY KEY, window_id bigint NOT NULL, hits integer NOT NULL);
CREATE FUNCTION weekly_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable record'; END $$;
CREATE TRIGGER submission_immutable BEFORE UPDATE OR DELETE ON reports_submission FOR EACH ROW EXECUTE FUNCTION weekly_immutable();
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON reports_audit FOR EACH ROW EXECUTE FUNCTION weekly_immutable();
CREATE FUNCTION weekly_report_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'UPDATE' AND OLD.first_submitted_at IS NOT NULL AND NEW.first_submitted_at IS DISTINCT FROM OLD.first_submitted_at THEN
  RAISE EXCEPTION 'first submission timestamp is immutable';
 END IF;
 IF NEW.latest_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM reports_submission s WHERE s.id=NEW.latest_id AND s.report_id=NEW.id) THEN
  RAISE EXCEPTION 'latest submission belongs to another report';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER report_guard BEFORE INSERT OR UPDATE ON reports_report FOR EACH ROW EXECUTE FUNCTION weekly_report_guard();
CREATE FUNCTION weekly_return_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM reports_submission s WHERE s.id=NEW.submission_id AND s.report_id=NEW.report_id)
 OR (NEW.resolved_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM reports_submission s WHERE s.id=NEW.resolved_id AND s.report_id=NEW.report_id)) THEN
  RAISE EXCEPTION 'return submission belongs to another report';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER return_guard BEFORE INSERT OR UPDATE ON reports_returnevent FOR EACH ROW EXECUTE FUNCTION weekly_return_guard();
CREATE FUNCTION weekly_export_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.manifest IS DISTINCT FROM OLD.manifest OR NEW.period_id != OLD.period_id OR NEW.creator_id != OLD.creator_id THEN
  RAISE EXCEPTION 'export snapshot is immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER export_guard BEFORE UPDATE ON reports_exportjob FOR EACH ROW EXECUTE FUNCTION weekly_export_guard();
'''


class Migration(migrations.Migration):
    dependencies = [('reports', '0001_initial')]
    operations = [migrations.RunSQL(SQL, reverse_sql='DROP TABLE reports_throttle; DROP TRIGGER submission_immutable ON reports_submission; DROP TRIGGER audit_immutable ON reports_audit; DROP TRIGGER report_guard ON reports_report; DROP TRIGGER return_guard ON reports_returnevent; DROP TRIGGER export_guard ON reports_exportjob; DROP FUNCTION weekly_immutable(); DROP FUNCTION weekly_report_guard(); DROP FUNCTION weekly_return_guard(); DROP FUNCTION weekly_export_guard();')]
