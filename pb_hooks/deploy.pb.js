/// <reference path="../pb_data/types.d.ts" />
// Health check outside /api/, kept out of the request log.
routerAdd("GET", "/up", (e) => {
  try {
    const row = new DynamicModel({n: 0});
    e.app.db().newQuery("SELECT count(*) AS n FROM _collections").one(row);
  } catch (_) {
    throw new ApiError(503, "The database is not available.", {});
  }
  return e.string(200, "OK");
}, $apis.skipSuccessActivityLog());
