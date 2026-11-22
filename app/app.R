library(shiny)

read_data <- function(file) {
  read.csv(file, stringsAsFactors = FALSE, check.names = FALSE,
           na.strings = character(), colClasses = "character", fileEncoding = "UTF-8")
}
base_counts <- read_data("baseline.csv")
base_counts$n <- as.integer(base_counts$n)
base_years <- read_data("annual.csv")
base_years$year <- as.integer(base_years$year)
base_years$n <- as.integer(base_years$n)
flow <- read_data("flow.csv")
studies <- read_data("studies.csv")
coverage <- read_data("coverage.csv")
baseline_notes <- read_data("baseline_notes.csv")
briefs <- read_data("briefs.csv")
run <- read_data("run.csv")
agreement <- read_data("agreement.csv")
families <- base_counts$category[base_counts$measure == "Driver family"]
paper_url <- "https://doi.org/10.1017/S1682098326100642"
osf_url <- "https://osf.io/c4kv3/"
if (!"inclusion_basis" %in% names(studies)) {
  primary <- grepl("(^| \\| )Primary($| \\| )", studies$queries)
  studies$inclusion_basis <- ifelse(primary & studies$status == "Verified inclusion", "Reviewer", "")
}
included <- studies[nzchar(studies$inclusion_basis), , drop = FALSE]
verified <- included[included$inclusion_basis == "Reviewer", , drop = FALSE]
ai_reviewed <- included[included$inclusion_basis == "AI", , drop = FALSE]
complete <- coverage[coverage$status == "Complete" & coverage$query == "Primary", , drop = FALSE]
last_search <- if (nrow(complete)) tail(complete$date, 1) else "Awaiting first search"

source_link <- function(label, url) tags$a(label, href = url, target = "_blank", rel = "noopener")
number_card <- function(value, caption) {
  tags$div(class = "metric", tags$strong(value), tags$span(caption))
}
message_box <- function(x) tags$p(class = "empty", x)

ui <- fluidPage(
  tags$head(tags$title("Affective polarization | Evidence monitor"),
            tags$meta(name = "viewport", content = "width=device-width, initial-scale=1"),
            tags$style(HTML(paste(readLines("style.css", warn = FALSE), collapse = "\n")))),
  tags$main(class = "page",
    tags$header(class = "masthead",
      tags$p(class = "source", "Kogel & Zinnatullin · European Political Science"),
      tags$h1("What drives affective polarization?"),
      tags$p(class = "intro", "Explore the published review and follow new evidence about the causes of partisan dislike."),
      tags$div(class = "metrics", number_card("135", "Published articles"),
               number_card(nrow(ai_reviewed), "AI-reviewed additions"),
               number_card(nrow(verified), "Reviewer-confirmed additions"),
               number_card(nrow(studies), "Records in update register"))),
    tabsetPanel(id = "tabs",
      tabPanel("Evidence map", includeHTML("evidence.html")),
      tabPanel("Study register",
        tags$div(class = "filters", textInput("query","Find a title, author or DOI",""),
          selectInput("screen_state","Review status", c("All", sort(unique(studies$status))), selectize = FALSE)),
        tags$p(class = "foot", "This register starts on 23 August 2025. The original study-level matrix is in ",
               source_link("Appendix C", "https://osf.io/c4kv3/files/yv4wp"), "."),
        uiOutput("record_list"), downloadButton("csv","Download filtered register")),
      tabPanel("Findings",
        tags$div(class = "section", selectInput("family","Driver family",families, selectize = FALSE),
                 uiOutput("findings"))),
      tabPanel("Methods & updates",
        tags$div(class = "section", tags$h2("Published selection flow"), tableOutput("flow"),
          tags$h2("Search coverage"), tags$p("Last complete primary search: ", last_search),
          tags$p(run$message[1]), uiOutput("coverage_view"), tags$h2("Review protocol"),
          tags$p("OpenAlex supplies the automated primary search and three sensitivity searches. The original review used Google Scholar and Scopus. Each search keeps its own hit count."),
          tags$p("The combined graph adds primary-search English-language journal articles dated after 22 August 2025. An ISSN match to SCImago and two agreeing full-text AI readings with checked evidence are required for automatic inclusion. The codes cover driver, outcome, design and countries."),
          tags$p("When a third independent AI reading can resolve a disagreement, the worker requests one within its existing budget. Unresolved cases stay in the register. Optional reviewer decisions take precedence. Each included record contributes once, in its publication year."),
          tags$p("Historical distributions and annual cells were transcribed from the published figures. Original screening was reported in aggregate. Two unspecified outcomes are inferred from the difference between Figures 4 and 5."),
          tags$p("The same human coder repeated the original driver classification after 15 days. Nominal Krippendorff’s alpha was 0.689. The update register retains both model readings for later comparison."),
          tags$h3("AI driver coding agreement"), tags$p(agreement$pairs[1], " paired inclusions with a classifiable driver. ",
            if (nzchar(agreement$alpha[1])) paste("Nominal alpha", agreement$alpha[1])
            else "Alpha requires variation in assigned driver families."),
          tags$p(source_link("Published review",paper_url), " · ", source_link("Replication data and R scripts", osf_url))
        ))
    )
  )
)

server <- function(input, output, session) {
  shown <- reactive({
    d <- studies
    if (!is.null(input$screen_state) && input$screen_state != "All") d <- d[d$status == input$screen_state, , drop = FALSE]
    if (!is.null(input$query) && nzchar(trimws(input$query))) {
      q <- tolower(trimws(input$query))
      d <- d[grepl(q, tolower(paste(d$title,d$authors,d$doi)), fixed = TRUE), , drop = FALSE]
    }
    d
  })
  output$record_list <- renderUI({
    d <- shown()
    if (!nrow(d)) return(message_box(if (nrow(studies)) "No matching studies." else "The first discovery run will populate this register."))
    tags$div(tags$p(class = "foot", sprintf("Showing %d of %d matching records. The download includes all matches.",min(75,nrow(d)),nrow(d))),
      lapply(seq_len(min(75,nrow(d))), function(i) {
        r <- d[i, ]
        url <- if (nzchar(r$doi)) paste0("https://doi.org/",r$doi) else paste0("https://openalex.org/",r$id)
        tags$details(tags$summary(source_link(r$title,url)," · ",r$status),
          tags$p(paste(r$authors,r$journal,r$date,sep=" · ")), tags$p(paste(r$id,r$queries,sep=" · ")),
          tags$p(paste(Filter(nzchar,c(r$driver,r$outcome,r$design,r$countries)),collapse=" · ")),
          tags$p(r$finding),tags$p(class="foot",r$note," ",r$locators),
          if (nzchar(r$inclusion_basis)) tags$p(class="foot",paste("Included in combined graph ·", r$inclusion_basis)))
      }))
  })
  output$csv <- downloadHandler(filename = function() "affective-polarization-register.csv",
    content = function(file) write.csv(shown(), file, row.names = FALSE, na = ""))
  output$findings <- renderUI({
    req(input$family)
    base <- baseline_notes$note[baseline_notes$driver == input$family]
    new <- briefs[briefs$driver == input$family, , drop = FALSE]
    tags$div(tags$h2("Published synthesis"), tags$p(base), tags$h2("Update draft"),
      if (!nrow(new)) message_box("A synthesis draft appears after the update job processes included findings for this driver.")
      else tags$div(tags$p(new$draft[1]), tags$p(class = "foot",new$date[1]," · AI draft for editorial review")))
  })
  output$flow <- renderTable(setNames(flow,c("Stage","Articles / records")), striped = TRUE, rownames = FALSE)
  output$coverage_view <- renderUI({
    if (!nrow(coverage)) return(message_box("The search history will appear after the first discovery run."))
    tagList(tableOutput("latest_search"), tags$h3("Current register by status"), tableOutput("stages"))
  })
  output$latest_search <- renderTable(tail(coverage,4), rownames = FALSE)
  output$stages <- renderTable({
    d <- as.data.frame(table(studies$status), stringsAsFactors = FALSE)
    names(d) <- c("Stage","Records")
    d
  }, rownames = FALSE)
}

shinyApp(ui, server)
