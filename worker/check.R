stopifnot(system2("python3", "worker/test_automatic_review.py") == 0L)
setwd("app")
obj <- source("app.R", local = TRUE)$value
stopifnot(inherits(obj, "shiny.appobj"), sum(base_years$n) == 135L,
          identical(as.integer(flow$n), c(761L,247L,187L,153L,135L)))
for (measure in c("Driver family","Outcome","Design","Journal quartile")) {
  stopifnot(sum(base_counts$n[base_counts$measure == measure]) == 135L)
}
for (family in families) {
  expected <- base_counts$n[base_counts$measure == "Driver family" & base_counts$category == family]
  stopifnot(sum(base_years$n[base_years$driver == family]) == expected)
}
for (form in c("Horizontal","Vertical","Both","Unspecified")) {
  expected <- base_counts$n[base_counts$measure == "Outcome" & base_counts$category == form]
  stopifnot(sum(base_years$n[base_years$outcome == form]) == expected)
}
shiny::testServer(server, {
  session$setInputs(family = families[1], query = "", screen_state = "All")
  stopifnot(!is.null(output$record_list))
})
message("Published margins and Shiny outputs passed.")

snapshot <- paste(readLines("evidence.html", warn = FALSE), collapse = "\n")
stopifnot(grepl("Horizontal · 57 articles", snapshot, fixed = TRUE),
          grepl(paste0("All forms · ", 135L + nrow(included), " articles"), snapshot, fixed = TRUE))
