# Read-only Innovus legacy dbGet exporter. Source this file in a loaded design.
# Example: ::ivflow::export -out /tmp/flow_db -depth 1 -expand {top/u_axi} 1
namespace eval ::ivflow { variable state; variable handles; variable counts }
proc ::ivflow::help {} {
    return {::ivflow::export -out DIR ?-root INSTANCE_PATH? ?-depth 1..64?
    ?-expand INSTANCE_PATH DEPTH? ... ?-stop INSTANCE_PATH? ...
    ?-empty_policy exclude|include? ?-max_instances 200000?
    ?-max_endpoints 2000000? ?-max_nets 2000000?
Paths use / between instances; dots and brackets are literal. Escape a literal
slash as ~1 and a literal tilde as ~0. Paths may start with the top name.
Only dbGet/getVersion are used; no GUI, netlist, placement or constraints change.
Empty / assign-only modules are excluded by default because their definition
and pin-direction provenance cannot be verified through this database API.}
}
proc ::ivflow::null {v} { expr {$v eq {} || $v eq {0x0} || $v eq {0}} }
proc ::ivflow::one {v} { lindex $v 0 }
proc ::ivflow::objects {ptr attr} {
    set v [dbGet $ptr.$attr]
    if {[llength $v] == 1 && [null [lindex $v 0]]} {return {}}
    return $v
}
proc ::ivflow::json_string {s} {
    set map [list \\ \\\\ \" \\\"]
    for {set i 0} {$i < 32} {incr i} {lappend map [format %c $i] [format {\u%04x} $i]}
    return \"[string map $map $s]\"
}
proc ::ivflow::json_array {items} {
    set out {}; foreach v $items {lappend out [json_string $v]}
    return \[[join $out ,]\]
}
proc ::ivflow::id {parts} {
    set out {}; foreach p $parts {lappend out [string map {~ ~0 / ~1} $p]}
    return [join $out /]
}
proc ::ivflow::path {value top} {
    if {$value eq {} || $value eq {/}} {return [list $top]}
    set out {}
    foreach p [split [string trim $value /] /] {
        if {$p eq {} || [regexp {~([^01]|$)} $p]} {error "Invalid instance path: $value"}
        lappend out [string map {~1 / ~0 ~} $p]
    }
    if {[lindex $out 0] ne $top} {set out [linsert $out 0 $top]}
    return $out
}
proc ::ivflow::beneath {p a} { expr {[lrange $p 0 [expr {[llength $a]-1}]] eq $a} }
proc ::ivflow::local_name {h parent} {
    set full [one [dbGet $h.name]]
    if {$parent eq [one [dbGet top.hInst]]} {return $full}
    set prefix "[one [dbGet $parent.name]]/"
    if {[string first $prefix $full] != 0} {error "Unexpected hierarchical name: $full"}
    return [string range $full [string length $prefix] end]
}
proc ::ivflow::resolve {parts} {
    set here [one [dbGet top.hInst]]
    foreach part [lrange $parts 1 end] {
        set found {}
        foreach child [objects $here hInsts] {
            if {[local_name $child $here] eq $part} {lappend found $child}
        }
        if {[llength $found] != 1} {error "Instance not found or ambiguous: [id $parts]"}
        set here [lindex $found 0]
    }
    return $here
}
proc ::ivflow::table {name columns} {
    variable state; variable handles; variable counts
    set f [open [file join $state(work) $name.tsv] w]
    fconfigure $f -encoding utf-8 -translation lf
    puts $f [join $columns \t]
    set handles($name) $f; set counts($name) 0
}
proc ::ivflow::row {name fields} {
    variable handles; variable counts
    set cells {}; foreach v $fields {lappend cells [json_string $v]}
    puts $handles($name) [join $cells \t]; incr counts($name)
}
proc ::ivflow::module_type {h parts} {
    if {[llength $parts] == 1} {return [lindex $parts 0]}
    return [one [dbGet $h.cell.name]]
}
proc ::ivflow::stats {h} {
    variable state
    if {![info exists state(stats,$h)]} {
        set children [objects $h hInsts]
        set leaves [llength [objects $h insts]]
        set state(stats,$h) [list $children $leaves]
    }
    return $state(stats,$h)
}
proc ::ivflow::want_expand {parts level children} {
    variable state
    if {[dict exists $state(stops) $parts]} {return 0}
    dict for {selected levels} $state(expands) {
        if {[beneath $selected $parts]} {return 1}
    }
    set budget $state(depth); set distance $level; set nearest -1
    dict for {selected levels} $state(expands) {
        if {[beneath $parts $selected] && [llength $selected] > $nearest} {
            set nearest [llength $selected]; set budget $levels
            set distance [expr {[llength $parts]-[llength $selected]}]
        }
    }
    return [expr {$distance < $budget && [llength $children] > 0}]
}
proc ::ivflow::visit {h parts level is_root} {
    variable state
    if {$level > 64} {error "Expansion exceeds 64 levels"}
    lassign [stats $h] children leaves
    set node [id $parts]; set type [module_type $h $parts]
    if {[llength $children] == 0 && $leaves == 0} {
        row diagnostics [list empty_module $node {} {Empty or assign-only module; definition/direction provenance is not verifiable}]
        if {$state(empty_policy) eq {exclude}} {return}
    }
    if {!$is_root && ![want_expand $parts $level $children]} {
        row nodes [list $node [json_array $parts] $type module frontier]
        lappend state(frontier) [list $h $parts]
        return
    }
    row scopes [list $node [json_array $parts] $type $leaves]
    lappend state(scopes) [list $h $parts]
    incr state(instances) [expr {$leaves+[llength $children]}]
    if {$state(instances) > $state(max_instances)} {error "Exceeded -max_instances; narrow root/depth"}
    if {$leaves} {row diagnostics [list leaf_cells $node {} "$leaves direct leaf cells are not traversed"]}
    foreach child $children {visit $child [concat $parts [list [local_name $child $h]]] [expr {$level+1}] 0}
}
proc ::ivflow::port_parts {name bus} {
    if {[null $bus]} {return [list $name {}]}
    if {![regexp {^(.*)\[(-?[0-9]+)\]$} $name -> base bit]} {error "Cannot identify vector bit from database name: $name"}
    return [list $base $bit]
}
proc ::ivflow::export_ports {h owner} {
    variable state
    set attrs {}
    foreach attr {hTerm.name term.bus term.inOutDir upHNet isTieHi isTieLo} {
        lappend attrs [dbGet $h.hInstTerms.$attr]
    }
    # An empty hInstTerms list is represented by 0x0 in dbGet.
    if {[llength [objects $h hInstTerms]] == 0} {return}
    lassign $attrs names buses dirs nets highs lows
    foreach a $attrs {if {[llength $a] != [llength $names]} {error "Port attribute length mismatch at $owner"}}
    foreach name $names bus $buses dir $dirs net $nets hi $highs lo $lows {
        lassign [port_parts $name $bus] port bit
        row ports [list $owner $port $bit $name $dir $net $hi $lo]
        incr state(endpoints)
        if {$state(endpoints) > $state(max_endpoints)} {error "Exceeded -max_endpoints"}
    }
}
proc ::ivflow::export_data {} {
    variable state
    set state(endpoints) 0; set net_count 0
    foreach item $state(frontier) {lassign $item h parts; export_ports $h [id $parts]}
    set boundary "[id $state(root)]/\$PORT"
    foreach p [lsort -unique [dbGet -e $state(root_ptr).hNets.hTerms]] {
        if {[null $p]} {continue}
        set name [one [dbGet $p.name]]
        lassign [port_parts $name [one [dbGet $p.term.bus]]] port bit
        row ports [list $boundary $port $bit $name [one [dbGet $p.term.inOutDir]] [one [dbGet $p.downHNet]] [one [dbGet $p.term.isTieHi]] [one [dbGet $p.term.isTieLo]]]
        incr state(endpoints)
        if {$state(endpoints) > $state(max_endpoints)} {error "Exceeded -max_endpoints"}
    }
    foreach item $state(scopes) {
        lassign $item h parts; set scope [id $parts]
        set nets [objects $h hNets]
        incr net_count [llength $nets]
        if {$net_count > $state(max_nets)} {error "Exceeded -max_nets"}
        if {[llength $nets]} {
            foreach net $nets name [dbGet $h.hNets.name] cn [dbGet $h.hNets.net] pg [dbGet $h.hNets.isPwrOrGnd] {
                row hnets [list $net $scope $name $cn $pg]
            }
        }
        if {$h eq $state(root_ptr) || ![llength [objects $h hInstTerms]]} {continue}
        foreach name [dbGet $h.hInstTerms.hTerm.name] dir [dbGet $h.hInstTerms.term.inOutDir] up [dbGet $h.hInstTerms.upHNet] down [dbGet $h.hInstTerms.downHNet] hi [dbGet $h.hInstTerms.isTieHi] lo [dbGet $h.hInstTerms.isTieLo] {
            row bridges [list $scope $name $dir $up $down $hi $lo]
        }
    }
}
proc ::ivflow::export {args} {
    variable state; variable handles; variable counts
    if {[llength $args] == 0 || $args eq {-help}} {puts [help]; return}
    catch {unset state}; catch {unset handles}; catch {unset counts}
    array set state {root_value {} depth 1 out {} empty_policy exclude max_instances 200000 max_endpoints 2000000 max_nets 2000000}
    set expands {}; set stops {}
    for {set i 0} {$i < [llength $args]} {incr i} {
        set flag [lindex $args $i]
        if {$flag eq {-expand}} {
            if {$i+2 >= [llength $args]} {error {-expand requires PATH DEPTH}}
            lappend expands [list [lindex $args [incr i]] [lindex $args [incr i]]]; continue
        }
        if {$i+1 >= [llength $args]} {error "Missing value for $flag"}
        set value [lindex $args [incr i]]
        switch -- $flag {
            -root {set state(root_value) $value}
            -out {set state(out) $value}
            -stop {lappend stops $value}
            -depth - -empty_policy - -max_instances - -max_endpoints - -max_nets {set state([string range $flag 1 end]) $value}
            default {error "Unknown option $flag\n[help]"}
        }
    }
    if {$state(out) eq {}} {error {-out is required}}
    foreach key {depth max_instances max_endpoints max_nets} {
        if {![string is integer -strict $state($key)] || $state($key) < 1} {error "$key must be a positive integer"}
    }
    if {$state(depth) > 64} {error {-depth must be 1..64}}
    if {$state(empty_policy) ni {exclude include}} {error {-empty_policy must be exclude or include}}
    set top [one [dbGet top.name]]
    if {[null $top]} {error {No loaded Innovus design}}
    set state(root) [path $state(root_value) $top]
    set state(root_ptr) [resolve $state(root)]
    set state(expands) {}; set state(stops) {}
    foreach item $expands {
        lassign $item value levels; set parts [path $value $top]
        if {![string is integer -strict $levels] || $levels < 1 || $levels > 64} {error {-expand depth must be 1..64}}
        if {![beneath $parts $state(root)]} {error "Expansion outside root: $value"}
        resolve $parts
        if {[dict exists $state(expands) $parts] && [dict get $state(expands) $parts] != $levels} {error "Conflicting expansion depth: $value"}
        dict set state(expands) $parts $levels
    }
    foreach value $stops {
        set parts [path $value $top]
        if {![beneath $parts $state(root)] || $parts eq $state(root)} {error "Stop outside/before root: $value"}
        resolve $parts; dict set state(stops) $parts 1
        dict for {expanded levels} $state(expands) {if {[beneath $expanded $parts]} {error "Expansion at/below stop: $value"}}
    }
    lassign [stats $state(root_ptr)] root_children root_leaves
    if {$state(empty_policy) eq {exclude} && ![llength $root_children] && !$root_leaves} {
        error {Selected root has no instances; verify its definition before using -empty_policy include}
    }
    # Excluded empty ancestors must not silently suppress a requested expansion.
    dict for {parts levels} $state(expands) {
        lassign [stats [resolve $parts]] children leaves
        if {$state(empty_policy) eq {exclude} && ![llength $children] && !$leaves} {error "Cannot expand excluded empty module: [id $parts]"}
    }
    set output [file normalize $state(out)]
    if {[file exists $output]} {error "Output already exists: $output"}
    file mkdir [file dirname $output]
    set state(work) "$output.working.[pid].[clock clicks]"
    file mkdir $state(work)
    set state(scopes) {}; set state(frontier) {}; set state(instances) 0
    set code [catch {
        table nodes {id path module_type kind status}
        table scopes {id path module_type direct_leaf_count}
        table ports {owner port bit pin direction hnet tie_hi tie_lo}
        table bridges {scope pin direction up_hnet down_hnet tie_hi tie_lo}
        table hnets {hnet scope name canonical_net power_ground}
        table diagnostics {kind id pin detail}
        row nodes [list "[id $state(root)]/\$PORT" [json_array $state(root)] [module_type $state(root_ptr) $state(root)] scope_boundary boundary]
        visit $state(root_ptr) $state(root) 0 1
        export_data
        foreach f [array names handles] {close $handles($f); unset handles($f)}
        set files {}
        foreach name {nodes scopes ports bridges hnets diagnostics} {
            lappend files "[json_string $name.tsv]:\{\"rows\":$counts($name),\"bytes\":[file size [file join $state(work) $name.tsv]]\}"
        }
        set selections {}
        dict for {parts levels} $state(expands) {lappend selections "\{\"path\":[json_string [id $parts]],\"depth\":$levels\}"}
        set stop_ids {}; dict for {parts v} $state(stops) {lappend stop_ids [id $parts]}
        set version unknown; catch {set version [getVersion]}
        set text "\{\"schema_version\":1,\"format\":\"innovus-hier-flow\",\"field_encoding\":\"json-string-tsv-v1\",\"status\":\"complete\",\"top\":[json_string $top],\"root\":[json_string [id $state(root)]],\"root_path\":[json_array $state(root)],\"depth\":$state(depth),\"expand\":\[[join $selections ,]\],\"stop\":[json_array $stop_ids],\"empty_policy\":[json_string $state(empty_policy)],\"innovus_version\":[json_string $version],\"exported_at\":[json_string [clock format [clock seconds] -format {%Y-%m-%dT%H:%M:%S%z}]],\"encountered_instances\":$state(instances),\"files\":\{[join $files ,]\}\}"
        set f [open [file join $state(work) manifest.json] w]; fconfigure $f -encoding utf-8 -translation lf
        puts $f $text; close $f
        if {[file exists $output]} {error "Output created during export: $output"}
        file rename $state(work) $output
    } result options]
    if {$code} {
        foreach name [array names handles] {catch {close $handles($name)}}
        return -options $options "$result\nPartial export retained at $state(work)"
    }
    puts "Innovus flow export complete: $output"
    return $output
}
