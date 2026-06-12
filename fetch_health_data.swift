// fetch_health_data.swift
// Reads Apple Health data via HealthKit and outputs JSON to stdout.
//
// One-time setup (run compile_health.sh):
//   swiftc fetch_health_data.swift -o fetch_health_data
//   codesign --sign - --entitlements healthkit.entitlements --force fetch_health_data
//
// First run will prompt for Health permission in System Preferences.

import HealthKit
import Foundation

guard HKHealthStore.isHealthDataAvailable() else {
    print("{\"ok\":false,\"error\":\"HealthKit not available\"}")
    exit(0)
}

let store = HKHealthStore()
let done = DispatchSemaphore(value: 0)

// Quantity types to request
let qIds: [HKQuantityTypeIdentifier] = [
    .stepCount, .activeEnergyBurned, .appleExerciseTime,
    .restingHeartRate, .bodyMass, .vo2Max,
]
var readTypes = Set<HKObjectType>()
for id in qIds {
    if let t = HKObjectType.quantityType(forIdentifier: id) { readTypes.insert(t) }
}
if let t = HKObjectType.categoryType(forIdentifier: .appleStandHour) { readTypes.insert(t) }

store.requestAuthorization(toShare: nil, read: readTypes) { granted, err in
    guard granted else {
        let msg = err?.localizedDescription ?? "denied"
        print("{\"ok\":false,\"error\":\"Health access denied: \(msg)\"}")
        done.signal(); return
    }

    let group = DispatchGroup()
    let cal   = Calendar.current
    let now   = Date()
    let sod   = cal.startOfDay(for: now)         // start of today
    let d7ago = cal.date(byAdding: .day, value: -6, to: sod)!

    let dateFmt = DateFormatter()
    dateFmt.locale     = Locale(identifier: "en_US_POSIX")
    dateFmt.dateFormat = "yyyy-MM-dd"

    var out: [String: Any] = ["ok": true, "date": dateFmt.string(from: now)]
    let lock = NSLock()
    func set(_ k: String, _ v: Any) { lock.lock(); out[k] = v; lock.unlock() }

    // ── Cumulative sum for today ──────────────────────────────────────────────
    func todaySum(_ id: HKQuantityTypeIdentifier, _ unit: HKUnit, _ key: String) {
        guard let type = HKQuantityType.quantityType(forIdentifier: id) else { return }
        group.enter()
        let pred = HKQuery.predicateForSamples(withStart: sod, end: now, options: .strictStartDate)
        store.execute(HKStatisticsQuery(quantityType: type, quantitySamplePredicate: pred, options: .cumulativeSum) { _, s, _ in
            if let v = s?.sumQuantity()?.doubleValue(for: unit) { set(key, v) }
            group.leave()
        })
    }

    // ── Most recent sample in last 30 days ───────────────────────────────────
    func recent(_ id: HKQuantityTypeIdentifier, _ unit: HKUnit, _ key: String, _ dateKey: String? = nil) {
        guard let type = HKQuantityType.quantityType(forIdentifier: id) else { return }
        group.enter()
        let pred = HKQuery.predicateForSamples(withStart: cal.date(byAdding: .day, value: -30, to: now)!, end: now)
        let sort = [NSSortDescriptor(key: HKSampleSortIdentifierEndDate, ascending: false)]
        store.execute(HKSampleQuery(sampleType: type, predicate: pred, limit: 1, sortDescriptors: sort) { _, samples, _ in
            if let s = samples?.first as? HKQuantitySample {
                set(key, s.quantity.doubleValue(for: unit))
                if let dk = dateKey { set(dk, dateFmt.string(from: s.endDate)) }
            }
            group.leave()
        })
    }

    // ── Run all queries ───────────────────────────────────────────────────────

    todaySum(.stepCount,          .count(),      "steps_today")
    todaySum(.activeEnergyBurned, .kilocalorie(), "calories_active_today")
    todaySum(.appleExerciseTime,  .minute(),      "exercise_minutes_today")

    recent(.bodyMass,        .pound(),                        "weight_lbs",  "weight_date")
    recent(.vo2Max,          HKUnit(from: "ml/kg*min"),       "vo2_max",     "vo2_max_date")
    recent(.restingHeartRate, HKUnit(from: "count/min"),      "resting_hr",  nil)

    // Stand hours today
    group.enter()
    if let standType = HKObjectType.categoryType(forIdentifier: .appleStandHour) {
        let pred = HKQuery.predicateForSamples(withStart: sod, end: now)
        store.execute(HKSampleQuery(sampleType: standType, predicate: pred, limit: HKObjectQueryNoLimit, sortDescriptors: nil) { _, samples, _ in
            let n = samples?.filter { ($0 as? HKCategorySample)?.value == HKCategoryValueAppleStandHour.stood.rawValue }.count ?? 0
            set("stand_hours_today", n)
            group.leave()
        })
    } else { group.leave() }

    // 7-day daily step counts
    group.enter()
    if let stepsType = HKQuantityType.quantityType(forIdentifier: .stepCount) {
        let pred  = HKQuery.predicateForSamples(withStart: d7ago, end: now)
        var comps = DateComponents(); comps.day = 1
        let q = HKStatisticsCollectionQuery(
            quantityType: stepsType,
            quantitySamplePredicate: pred,
            options: .cumulativeSum,
            anchorDate: sod,
            intervalComponents: comps
        )
        q.initialResultsHandler = { _, col, _ in
            var days: [[String: Any]] = []
            col?.enumerateStatistics(from: d7ago, to: now) { stats, _ in
                days.append([
                    "date":  dateFmt.string(from: stats.startDate),
                    "steps": Int(stats.sumQuantity()?.doubleValue(for: .count()) ?? 0),
                ])
            }
            set("steps_7day", days)
            group.leave()
        }
        store.execute(q)
    } else { group.leave() }

    // ── Emit JSON ─────────────────────────────────────────────────────────────
    group.notify(queue: .global()) {
        // Tidy up numeric types
        if let v = out["steps_today"]            as? Double { out["steps_today"]             = Int(v) }
        if let v = out["calories_active_today"]  as? Double { out["calories_active_today"]   = Int(v) }
        if let v = out["exercise_minutes_today"] as? Double { out["exercise_minutes_today"]  = Int(v) }
        if let v = out["weight_lbs"]             as? Double { out["weight_lbs"]              = round(v * 10) / 10 }
        if let v = out["vo2_max"]                as? Double { out["vo2_max"]                 = round(v * 10) / 10 }
        if let v = out["resting_hr"]             as? Double { out["resting_hr"]              = Int(v) }

        if let data = try? JSONSerialization.data(withJSONObject: out, options: .sortedKeys),
           let str  = String(data: data, encoding: .utf8) {
            print(str)
        } else {
            print("{\"ok\":false,\"error\":\"JSON serialization failed\"}")
        }
        done.signal()
    }
}

done.wait()
