/**
 * Overtime.ag College Football offering capture.
 *
 * Usage: log in to https://overtime.ag/sports#/ in a browser, open the
 * "COLLEGE FB (1H/2H/Q)" league, then paste this in the DevTools console (or run it via a
 * browser-automation `evaluate`). It returns a JSON string containing every line for every
 * period (Game / 1st Half / 1st Quarter / ...) held in the page's Angular model. Save it as
 *   data/raw/overtime/ncaaf_lines_<snapshot>.json
 * and run `ncaaf-clv overtime <file>`.
 *
 * Only a whitelist of fields is exported; nothing account-related is touched.
 */
(function captureOvertimeCollegeFootball() {
  const FIELDS = [
    "GameNum", "ParentGameNum", "Team1ID", "Team2ID", "Team1RotNum", "Team2RotNum",
    "GameDateTime", "GameDateTimeString", "PeriodNumber", "PeriodDescription",
    "FavoredTeamID", "Spread", "SpreadAdj1", "SpreadAdj2", "TotalPoints", "TtlPtsAdj1",
    "TtlPtsAdj2", "MoneyLine1", "MoneyLine2", "Team1TotalPoints", "Team1TtlPtsAdj1",
    "Team1TtlPtsAdj2", "Team2TotalPoints", "Team2TtlPtsAdj1", "Team2TtlPtsAdj2", "LineSeq",
    "Status", "SpreadLineStatus", "MoneyLineStatus", "TotalsLineStatus",
    "TeamTotalsLineStatus", "Comments", "SportSubTypeId", "SportSubType", "PeriodWagerCutoff",
  ];
  const anchor = document.querySelector(".offering-input");
  if (!anchor || typeof angular === "undefined") {
    throw new Error("Offering grid not found - open the COLLEGE FB league first");
  }
  let scope = angular.element(anchor).scope();
  while (scope && !("league" in scope)) scope = scope.$parent;
  if (!scope) throw new Error("league scope not found");
  const league = scope.league;
  const lines = (league.Lines || []).map((l) => {
    const o = {};
    for (const f of FIELDS) o[f] = l[f] === undefined ? null : l[f];
    return o;
  });
  const dump = {
    captured_at: new Date().toISOString(),
    league: {
      SportType: league.SportType,
      SportSubType: league.SportSubType,
      SportSubTypeId: league.SportSubTypeId,
      ScheduleDateString: league.ScheduleDateString,
    },
    periods: (league.Periods || []).map((p) => ({
      PeriodNumber: p.PeriodNumber,
      PeriodDescription: p.PeriodDescription,
    })),
    lines,
  };
  return JSON.stringify(dump);
})();
