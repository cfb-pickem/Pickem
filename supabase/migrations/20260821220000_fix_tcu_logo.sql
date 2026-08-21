-- The TCU row was the only one of 254 logos pointing at Wikimedia. Wikimedia now
-- answers that thumbnail URL with HTTP 400 ("Use thumbnail sizes listed on…"),
-- which Chrome surfaces as ERR_BLOCKED_BY_ORB, so TCU rendered as a broken image
-- everywhere it appeared. Repointed at the same host every other logo uses.
update public.logos
   set "LogoURL" = 'https://raw.githubusercontent.com/abrelsfo/sportsJSON/master/collegeFootball/logos/TCU.png'
 where "Team" = 'TCU';
