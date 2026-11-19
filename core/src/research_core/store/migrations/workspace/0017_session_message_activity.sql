-- Latest-chat selection and the selected chat's preview read admissions in creation order.
CREATE INDEX client_admissions_session_message_time ON client_admissions(session_id, created_at)
WHERE kind='message';
