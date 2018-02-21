from scapy import config
import sys, getopt
config.Conf.load_layers.remove("x509")
import shlex
from scapy.all import *
from Maths.Bounds import *
from Packets.Utils import *
from Graph.Operations import *
from Graph.Visualization import *
from Graph.Statistics import *


# Link batches
batch_link_probe_size = 30

total_probe_sent = 0

default_stop_on_consecutive_stars = 3

def increment_probe_sent(n):
    global total_probe_sent
    total_probe_sent = total_probe_sent + n

def update_graph_from_replies(g, replies):
    for probe, reply in replies:
        src_ip = extract_src_ip(reply)
        flow_id = extract_flow_id_reply(reply)
        ttl = extract_ttl(probe)
        # Update the graph
        g = update_graph(g, src_ip, ttl, flow_id)

def reconnect_successors(g, destination, ttl):
    reconnect_impl(g, destination, ttl, ttl + 1)

def reconnect_predecessors(g, destination, ttl):
    reconnect_impl(g, destination, ttl, ttl-1)

def reconnect_impl(g, destination, ttl, ttl2):
    ttls_flow_ids = g.vertex_properties["ttls_flow_ids"]
    if ttl > ttl2 :
        no_neighbors_vertices = find_no_predecessor_vertices(g, ttl)
    else:
        no_neighbors_vertices = find_no_successor_vertices(g, ttl)
    check_neighbors_probes = []
    for v in no_neighbors_vertices:
        flow_id = ttls_flow_ids[v][ttl][0]
        check_neighbors_probes.append(build_probe(destination, ttl2, flow_id))
    replies, answered = sr(check_neighbors_probes, timeout=5, verbose=False)
    increment_probe_sent(len(check_neighbors_probes))
    update_graph_from_replies(g, replies)




def execute_phase1(g, destination, vertex_confidence):
    global default_stop_on_consecutive_stars
    has_found_longest_path_to_destination = False
    consecutive_only_star = 0
    ttl = 1
    while not has_found_longest_path_to_destination:
        if consecutive_only_star == default_stop_on_consecutive_stars:
            print str(default_stop_on_consecutive_stars) + " consecutive hop with only stars found, stopping the algorithm."
            exit(0)
        phase1_probes = get_phase_1_probe(destination, ttl, vertex_confidence)
        replies, unanswered = sr(phase1_probes, timeout=5, verbose=True)
        increment_probe_sent(len(phase1_probes))
        if len(replies) == 0:
            consecutive_only_star = consecutive_only_star + 1
        else:
            consecutive_only_star = 0
        for probe in unanswered:
            flow_id = extract_flow_id_probe(probe)
            src_ip = "* * * " + str(ttl)
            # Update the graph
            g = update_graph(g, src_ip, ttl, flow_id)
        replies_only_from_destination = True
        for probe, reply in replies:
            src_ip = extract_src_ip(reply)
            flow_id = extract_flow_id_reply(reply)
            probe_ttl = extract_ttl(probe)
            # Update the graph
            g = update_graph(g, src_ip, probe_ttl, flow_id)
            # graph_topology_draw(g)
            print src_ip
            if src_ip != destination:
                replies_only_from_destination = False
        if replies_only_from_destination:
            has_found_longest_path_to_destination = True
        ttl = ttl + 1


def execute_phase3(g, destination, llb, vertex_confidence, limit_link_probes, with_inference):
    ttls_flow_ids = g.vertex_properties["ttls_flow_ids"]
    #llb : List of load balancer lb
    for lb in llb:
        # nint is the number of already discovered interfaces
        for ttl, nint in lb.get_ttl_vertices_number().iteritems():
            # TODO Parametrize the nks
            nprobe_sent = find_probes_sent(g, ttl)
            hypothesis = nint + 1
            while nprobe_sent < nk99[hypothesis]:
                next_flow_id = find_max_flow_id(g, ttl)
                nprobes = nk99[hypothesis] - nprobe_sent
                probes  = []
                # Generate the nprobes
                for j in range(1, nprobes + 1):
                    probes.append(build_probe(destination, ttl, next_flow_id + j))
                increment_probe_sent(len(probes))
                replies, answered = sr(probes, timeout=1, verbose=False)
                for probe, reply in replies:
                    src_ip = extract_src_ip(reply)
                    flow_id = extract_flow_id_reply(reply)
                    probe_ttl = extract_ttl(probe)
                    if is_new_ip(g, src_ip):
                        hypothesis = hypothesis + 1
                    # Update the graph
                    g = update_graph(g, src_ip, probe_ttl, flow_id)
                nprobe_sent = nprobe_sent + nprobes

            if with_inference:
                if len(lb.get_ttl_vertices_number()) == 1:
                    apply_converging_heuristic(g, ttl, forward=True, backward=True)
                elif ttl == max(lb.get_ttl_vertices_number().keys()):
                    apply_converging_heuristic(g, ttl, forward=True, backward=False)
    # Second round, reconnect all the nodes that have no successors or no predecessors
    for lb in llb:
        for ttl, nint in lb.get_ttl_vertices_number().iteritems():
            reconnect_predecessors(g, destination, ttl)
            reconnect_successors(g, destination, ttl)
    # Third round, try to infer the missing links if necessary from the flows you already have
    for lb in llb:
        for ttl, nint in lb.get_ttl_vertices_number().iteritems():
            if ttl == min(lb.get_ttl_vertices_number().keys()):
                continue
            # Check if this TTL is a divergence point or a convergence point
            if is_a_divergent_ttl(g, ttl):
                has_to_probe_more = apply_multiple_predecessors_heuristic(g, ttl)
            else:
                has_to_probe_more = apply_multiple_successors_heuristic(g, ttl - 1)
            if has_to_probe_more:
                # Here it is more complicated, we have to infer multiple predecessors
                missing_flows = find_missing_flows(g, ttl, ttl - 1)
                check_predecessor_probes = []
                for flow_id in missing_flows:
                    check_predecessor_probes.append(build_probe(destination, ttl-1, flow_id))
                increment_probe_sent(len(check_predecessor_probes))
                replies, answered = sr(check_predecessor_probes, timeout=1, verbose=False)
                update_graph_from_replies(g, replies)

    # Fourth round, try to infer the missing links by generating new flows
    # This number is parametrable
    links_probes_sent = 0
    has_to_apply_common_successors_heuristics = True
    while links_probes_sent < limit_link_probes and has_to_apply_common_successors_heuristics:
        has_to_apply_common_successors_heuristics = False
        for lb in llb:
            # Filter the ttls where there are multiple predecessors
            for ttl, nint in lb.get_ttl_vertices_number().iteritems():
                if ttl == min(lb.get_ttl_vertices_number().keys()):
                    continue
                # Check if this TTL is a divergence point or a convergence point
                if is_a_divergent_ttl(g, ttl):
                    has_to_probe_more = apply_multiple_predecessors_heuristic(g, ttl)
                else:
                    has_to_probe_more = apply_multiple_successors_heuristic(g, ttl - 1)
                if has_to_probe_more:
                    has_to_apply_common_successors_heuristics = True
                    has_discovered_new_link = True
                    # Generate probes new flow_ids
                    while has_discovered_new_link and links_probes_sent < limit_link_probes:
                        has_discovered_new_link = False
                        # Privilegiate flows that are already at ttl - 1
                        check_links_probes = []
                        overflows = find_missing_flows(g, ttl-1, ttl)
                        for flow in overflows:
                            check_links_probes.append(build_probe(destination, ttl, flow))
                        next_flow_id_overflows = 0
                        if len(overflows) != 0:
                            next_flow_id_overflows = max(overflows)
                        next_flow_id = max(find_max_flow_id(g, ttl), next_flow_id_overflows)
                        for i in range(1, batch_link_probe_size+1-len(overflows)):
                            check_links_probes.append(build_probe(destination, ttl, next_flow_id + i))
                        increment_probe_sent(len(check_links_probes))
                        replies, answered = sr(check_links_probes, timeout=1, verbose=False)
                        discovered = 0

                        for probe, reply in replies:
                            src_ip = extract_src_ip(reply)
                            flow_id = extract_flow_id_reply(reply)
                            probe_ttl = extract_ttl(probe)
                            if has_discovered_edge(g, src_ip, probe_ttl, flow_id):
                                has_discovered_new_link = True
                                discovered = discovered + 1
                            # Update the graph
                            g = update_graph(g, src_ip, probe_ttl, flow_id)
                        links_probes_sent = links_probes_sent + batch_link_probe_size
                        # With the new flows generated, find the missing flows at ttl-1
                        check_missing_flow_probes = []
                        missing_flows = find_missing_flows(g, ttl, ttl-1)
                        for flow in missing_flows:
                            check_missing_flow_probes.append(build_probe(destination, ttl - 1, flow))
                        increment_probe_sent(len(check_missing_flow_probes))
                        replies, answered = sr(check_missing_flow_probes, timeout=5, verbose=False)
                        for probe, reply in replies:
                            src_ip = extract_src_ip(reply)
                            flow_id = extract_flow_id_reply(reply)
                            probe_ttl = extract_ttl(probe)
                            # Update the graph
                            if has_discovered_edge(g, src_ip, probe_ttl, flow_id):
                                has_discovered_new_link = True
                                discovered = discovered + 1
                            g = update_graph(g, src_ip, probe_ttl, flow_id)
                        links_probes_sent = links_probes_sent + len(check_missing_flow_probes)
                        dump_flows(g)

    # Apply final heuristics based on symetry to infer links
    if with_inference:
        remove_parallel_edges(g)
        for lb in llb:
            # Filter the ttls where there are multiple predecessors
            for ttl, nint in lb.get_ttl_vertices_number().iteritems():
                apply_symmetry_heuristic(g, ttl, 2)
    remove_parallel_edges(g)


def main(argv):
    # default values
    protocol = "udp"
    limit_edges = 500
    vertex_confidence = 99
    output_file = ""
    with_inference = False
    save_flows_infos = True
    try:
        opts, args = getopt.getopt(argv, "ho:c:b:i", ["help","ofile=", "vertex-confidence=", "edge-budget=", "with-inference"])
    except getopt.GetoptError:
        print 'Usage : 3-phase-mda.py -o <outputfile> (*.xml, *.json, default: draw_graph) -c <vertex-confidence> (95, 99) -b <edge-budget> (default:500) <destination>'
        sys.exit(2)
    for opt, arg in opts:
        if opt == '-h':
            print 'Usage : 3-phase-mda.py -o <outputfile> (*.xml, *.json, default: draw_graph) -c <vertex-confidence> (95, 99) -b <edge-budget> (default:500) <destination>'
            sys.exit()
        elif opt in ("-o", "--ofile"):
            output_file = arg
        elif opt in ("-c", "--vertex-confidence"):
            vertex_confidence = int(arg)
        elif opt in ("-b", "--edge-budget"):
            limit_edges = int(arg)
        elif opt in ("-i", "--with-inference"):
            with_inference = True
    destination  = args[0]

    g = init_graph()
    # 3 phases in the algorithm :
    # 1-2) hop by hop 6 probes to discover length + position of LB
    # 3) Load balancer discovery

    print "Starting phase 1 and 2 : finding a length to the destination and the place of the diamonds"
    # Phase 1
    execute_phase1(g, destination, vertex_confidence)
    #graph_topology_draw(g)

    #Phase 2
    llb = extract_load_balancers(g)

    # We assume symmetry until we discover that it is not.
    # First reach the nks for this corresponding hops.
    print "Starting phase 3 : finding the topology of the discovered diamonds"
    execute_phase3(g, destination, llb, vertex_confidence, limit_edges, with_inference)
    remove_self_loops(g)
    clean_stars(g)
    print "Total probe sent : " + str(total_probe_sent)
    print "Percentage of edges inferred : " + str(get_percentage_of_inferred(g))  + "%"
    print "Phase 3 finished"



    if output_file == "":
        graph_topology_draw_with_inferred(g)
    else:
        if save_flows_infos:
            # Get source info
            skeleton = build_ip_probe(destination, 1)
            source_ip = extract_src_ip(skeleton)
            enrich_flows(g, source_ip, destination, protocol, sport, dport)
        g.save(output_file)
    dump_results(g, destination)
    #full_mda_g = load_graph("/home/osboxes/CLionProjects/fakeRouteC++/resources/ple2.planet-lab.eu_125.155.82.17.xml")
    #graph_topology_draw(full_mda_g)
if __name__ == "__main__":
    config.conf.L3socket = L3RawSocket
    main(sys.argv[1:])
