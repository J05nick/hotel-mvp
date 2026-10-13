-- Demo data: four rooms in different states, so the calendar has something to show.
-- Idempotent: safe to run repeatedly (rooms are keyed by their unique `number`).
INSERT INTO rooms (number, name, capacity, price_per_block_cents, status) VALUES
    ('101', 'Garden suite', 2, 1500, 'available'),
    ('102', 'Sea view',     4, 2500, 'cleaning'),
    ('103', 'Attic studio', 2, 1000, 'maintenance'),
    ('104', 'Courtyard',    3, 1800, 'available')
ON CONFLICT (number) DO NOTHING;
