-- Make the customer one-time code un-guessable.
--
-- A six-digit code is 1,000,000 possibilities and `verify-otp` is anonymous,
-- so the code is the *entire* authentication for a customer. Measured on
-- 2026-08-19 against the live service: **30 wrong codes in 0.1 seconds, every
-- one answered 401, with no throttle and no lockout.** At that rate the whole
-- space falls inside the code's own ten-minute life, and the account is taken
-- over without the owner ever seeing a prompt.
--
-- The password path already solved this — `velaris_users.failed_attempts` plus
-- a lockout — so this is the same control, not a new idea. Five wrong guesses
-- retires the code and the person asks for another; the counter resets on every
-- newly issued code and on a correct one.
--
-- A per-account counter rather than only a rate limit by address: an attacker
-- with a spread of addresses walks past an IP limit, and an IP limit also
-- punishes a household or an office behind one NAT. The rate limit is added at
-- the route as well, but this is the control that actually bounds the guess.
ALTER TABLE portal_customers
    ADD COLUMN IF NOT EXISTS otp_attempts INTEGER NOT NULL DEFAULT 0;
