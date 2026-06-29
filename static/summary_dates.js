// Quick-range buttons for the /summary page (PRD §2.6).
//
// 6 buttons: 本周 / 上周 / 本月 / 上月 / 本季 / 本年.
// On click, compute a (start, end) YYYY-MM-DD pair in the browser's
// local timezone and navigate to /summary?start=...&end=... so the
// server's parse_summary_dates can validate it.
//
// The endpoint already tolerates end = today + 1 day (PRD §2.6.3).
(function () {
  "use strict";

  function pad(n) {
    return n < 10 ? "0" + n : "" + n;
  }

  function fmt(d) {
    // Local-date YYYY-MM-DD (NOT UTC — spec §0.3: 用户浏览器时区展示).
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function startOfDay(d) {
    var x = new Date(d);
    x.setHours(0, 0, 0, 0);
    return x;
  }

  function addDays(d, n) {
    var x = new Date(d);
    x.setDate(x.getDate() + n);
    return x;
  }

  // Monday = 1 ... Sunday = 7  (ISO-8601). Local-time computation.
  function startOfWeek(today) {
    var dow = today.getDay() || 7;  // 0 (Sun) → 7
    return addDays(today, 1 - dow);
  }

  // Quick-range specs:
  //   本周:    Mon .. today
  //   上周:    last Mon .. last Sun
  //   本月:    1st .. today
  //   上月:    prev-month 1st .. prev-month last day
  //   本季:    quarter start .. today
  //   本年:    Jan 1 .. today
  function rangeFor(name) {
    var today = startOfDay(new Date());
    switch (name) {
      case "this-week": {
        var s = startOfWeek(today);
        return [s, today];
      }
      case "last-week": {
        var thisStart = startOfWeek(today);
        var s = addDays(thisStart, -7);
        var e = addDays(thisStart, -1);
        return [s, e];
      }
      case "this-month": {
        var s = new Date(today.getFullYear(), today.getMonth(), 1);
        return [s, today];
      }
      case "last-month": {
        var s = new Date(today.getFullYear(), today.getMonth() - 1, 1);
        var e = new Date(today.getFullYear(), today.getMonth(), 0);  // 0th = last day of prev month
        return [s, e];
      }
      case "this-quarter": {
        var q = Math.floor(today.getMonth() / 3);  // 0..3
        var s = new Date(today.getFullYear(), q * 3, 1);
        return [s, today];
      }
      case "this-year": {
        var s = new Date(today.getFullYear(), 0, 1);
        return [s, today];
      }
      default:
        return null;
    }
  }

  function navigate(name) {
    var r = rangeFor(name);
    if (!r) return;
    var url = "/summary?start=" + fmt(r[0]) + "&end=" + fmt(r[1]);
    window.location.assign(url);
  }

  function init() {
    var buttons = document.querySelectorAll("[data-quick]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", function (e) {
        var name = e.currentTarget.getAttribute("data-quick");
        navigate(name);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
