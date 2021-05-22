from qiskit.dagcircuit.dagcircuit import DAGCircuit
from qiskit.converters import circuit_to_dag, dag_to_circuit
import gurobipy as gp
import numpy as np
import math
from qiskit import QuantumCircuit, QuantumRegister

class MIP_Model(object):
    def __init__(self, n_vertices, edges, vertex_ids, id_vertices, num_subcircuit, max_subcircuit_qubit, num_qubits, max_cuts):
        self.check_graph(n_vertices, edges)
        self.n_vertices = n_vertices
        self.edges = edges
        self.n_edges = len(edges)
        self.vertex_ids = vertex_ids
        self.id_vertices = id_vertices
        self.num_subcircuit = num_subcircuit
        self.max_subcircuit_qubit = max_subcircuit_qubit
        self.num_qubits = num_qubits
        self.max_cuts = max_cuts

        self.model = gp.Model(name='cut_searching')
        self.model.params.OutputFlag = 0

        self.vertex_weight = {}
        for node in self.vertex_ids:
            qargs = node.split(' ')
            num_in_qubits = 0
            for qarg in qargs:
                if int(qarg.split(']')[1]) == 0:
                    num_in_qubits += 1
            self.vertex_weight[node] = num_in_qubits

        # Indicate if a vertex is in some subcircuit
        self.vertex_var = []
        for i in range(num_subcircuit):
            subcircuit_y = []
            for j in range(self.n_vertices):
                j_in_i = self.model.addVar(lb=0.0, ub=1.0, vtype=gp.GRB.BINARY)
                subcircuit_y.append(j_in_i)
            self.vertex_var.append(subcircuit_y)

        # Indicate if an edge has one and only one vertex in some subcircuit
        self.edge_var = []
        for i in range(num_subcircuit):
            subcircuit_x = []
            for j in range(self.n_edges):
                v = self.model.addVar(lb=0.0, ub=1.0, vtype=gp.GRB.BINARY)
                subcircuit_x.append(v)
            self.edge_var.append(subcircuit_x)
        
        # constraint: each vertex in exactly one subcircuit
        for v in range(self.n_vertices):
            self.model.addConstr(gp.quicksum([self.vertex_var[i][v] for i in range(num_subcircuit)]), gp.GRB.EQUAL, 1)
        
        # constraint: edge_var=1 indicates one and only one vertex of an edge is in subcircuit
        # edge_var[subcircuit][edge] = vertex_var[subcircuit][u] XOR vertex_var[subcircuit][v]
        for i in range(num_subcircuit):
            for e in range(self.n_edges):
                u, v = self.edges[e]
                u_vertex_var = self.vertex_var[i][u]
                v_vertex_var = self.vertex_var[i][v]
                self.model.addConstr(self.edge_var[i][e] <= u_vertex_var+v_vertex_var)
                self.model.addConstr(self.edge_var[i][e] >= u_vertex_var-v_vertex_var)
                self.model.addConstr(self.edge_var[i][e] >= v_vertex_var-u_vertex_var)
                self.model.addConstr(self.edge_var[i][e] <= 2-u_vertex_var-v_vertex_var)

        # Better (but not best) symmetry-breaking constraints
        #   Force small-numbered vertices into small-numbered subcircuits:
        #     v0: in subcircuit 0
        #     v1: in c0 or c1
        #     v2: in c0 or c1 or c2
        #     ....
        for vertex in range(num_subcircuit):
            self.model.addConstr(gp.quicksum([self.vertex_var[subcircuit][vertex] for subcircuit in range(vertex+1,num_subcircuit)]) == 0)
        
        # NOTE: add 0.1 for numerical stability
        self.num_cuts = self.model.addVar(lb=0, ub=self.max_cuts+0.1, vtype=gp.GRB.INTEGER, name='num_cuts')
        self.model.addConstr(self.num_cuts == 
        gp.quicksum(
            [self.edge_var[subcircuit][i] for i in range(self.n_edges) for subcircuit in range(num_subcircuit)]
            )/2)
        
        num_effective_qubits = []
        for subcircuit in range(num_subcircuit):
            subcircuit_original_qubit = self.model.addVar(lb=0, ub=self.max_subcircuit_qubit, vtype=gp.GRB.INTEGER, name='subcircuit_input_%d'%subcircuit)
            self.model.addConstr(subcircuit_original_qubit ==
            gp.quicksum([self.vertex_weight[id_vertices[i]]*self.vertex_var[subcircuit][i]
            for i in range(self.n_vertices)]))
            
            subcircuit_rho_qubits = self.model.addVar(lb=0, ub=self.max_subcircuit_qubit, vtype=gp.GRB.INTEGER, name='subcircuit_rho_qubits_%d'%subcircuit)
            self.model.addConstr(subcircuit_rho_qubits ==
            gp.quicksum([self.edge_var[subcircuit][i] * self.vertex_var[subcircuit][self.edges[i][1]]
            for i in range(self.n_edges)]))

            subcircuit_O_qubits = self.model.addVar(lb=0, ub=self.max_subcircuit_qubit, vtype=gp.GRB.INTEGER, name='subcircuit_O_qubits_%d'%subcircuit)
            self.model.addConstr(subcircuit_O_qubits ==
            gp.quicksum([self.edge_var[subcircuit][i] * self.vertex_var[subcircuit][self.edges[i][0]]
            for i in range(self.n_edges)]))

            # self.model.addConstr(subcircuit_rho_qubits + subcircuit_O_qubits <= 5)

            subcircuit_d = self.model.addVar(lb=0.1, ub=self.max_subcircuit_qubit, vtype=gp.GRB.INTEGER, name='subcircuit_d_%d'%subcircuit)
            self.model.addConstr(subcircuit_d == subcircuit_original_qubit + subcircuit_rho_qubits)

            # subcircuit_size = self.model.addVar(lb=0.1, ub=int(self.n_vertices/2), vtype=GRB.INTEGER, name='subcircuit_size_%d'%subcircuit)
            # self.model.addConstr(subcircuit_size == quicksum([self.vertex_var[subcircuit][v] for v in range(self.n_vertices)]))

            num_effective_qubits.append(subcircuit_d-subcircuit_O_qubits)
            
            if subcircuit>0:
                lb = 0
                ub = self.num_qubits+2*20
                ptx, ptf = self.pwl_exp(lb=lb,ub=ub,base=2,integer_only=True)
                build_cost_exponent = self.model.addVar(lb=lb, ub=ub, vtype=gp.GRB.INTEGER, name='build_cost_exponent_%d'%subcircuit)
                self.model.addConstr(build_cost_exponent == gp.quicksum(num_effective_qubits)+2*self.num_cuts)
            #     self.model.setPWLObj(build_cost_exponent, ptx, ptf)

        self.model.setObjective(self.num_cuts,gp.GRB.MINIMIZE)
        self.model.update()
    
    def pwl_exp(self, lb, ub, base, integer_only):
        # Piecewise linear approximation of base**x
        ptx = []
        ptf = []

        x_range = range(lb,ub+1) if integer_only else np.linspace(lb,ub,200)
        # print('x_range : {}, integer_only : {}'.format(x_range,integer_only))
        for x in x_range:
            y = base**x
            ptx.append(x)
            ptf.append(y)
        return ptx, ptf
    
    def check_graph(self, n_vertices, edges):
        # 1. edges must include all vertices
        # 2. all u,v must be ordered and smaller than n_vertices
        vertices = set([i for (i, _) in edges])
        vertices |= set([i for (_, i) in edges])
        assert(vertices == set(range(n_vertices)))
        for u, v in edges:
            assert(u < v)
            assert(u < n_vertices)
    
    def solve(self,min_postprocessing_cost):
        # print('solving for %d subcircuits'%self.num_subcircuit)
        # print('model has %d variables, %d linear constraints,%d quadratic constraints, %d general constraints'
        # % (self.model.NumVars,self.model.NumConstrs, self.model.NumQConstrs, self.model.NumGenConstrs))
        try:
            self.model.Params.TimeLimit = 300
            self.model.Params.cutoff = min_postprocessing_cost
            self.model.optimize()
        except (gp.GurobiError, AttributeError, Exception) as e:
            print('Caught: ' + e.message)
        
        if self.model.solcount > 0:
            self.objective = None
            self.subcircuits_vertices = []
            self.optimal = (self.model.Status == gp.GRB.OPTIMAL)
            self.runtime = self.model.Runtime
            self.node_count = self.model.nodecount
            self.mip_gap = self.model.mipgap
            self.objective = self.model.ObjVal

            for i in range(self.num_subcircuit):
                subcircuit_vertices = []
                for j in range(self.n_vertices):
                    if abs(self.vertex_var[i][j].x) > 1e-4:
                        subcircuit_vertices.append(self.id_vertices[j])
                self.subcircuits_vertices.append(subcircuit_vertices)
            assert sum([len(x) for x in self.subcircuits_vertices])==self.n_vertices

            cut_edges_idx = []
            cut_edges = []
            for i in range(self.num_subcircuit):
                for j in range(self.n_edges):
                    if abs(self.edge_var[i][j].x) > 1e-4 and j not in cut_edges_idx:
                        cut_edges_idx.append(j)
                        u, v = self.edges[j]
                        cut_edges.append((self.id_vertices[u], self.id_vertices[v]))
            self.cut_edges = cut_edges
            return True
        else:
            # print('Infeasible')
            return False

def read_circ(circuit):
    dag = circuit_to_dag(circuit)
    edges = []
    node_name_ids = {}
    id_node_names = {}
    vertex_ids = {}
    curr_node_id = 0
    qubit_gate_counter = {}
    for qubit in dag.qubits:
        qubit_gate_counter[qubit] = 0
    for vertex in dag.topological_op_nodes():
        if len(vertex.qargs) != 2:
            raise Exception('vertex does not have 2 qargs!')
        arg0, arg1 = vertex.qargs
        vertex_name = '%s[%d]%d %s[%d]%d' % (arg0.register.name, arg0.index, qubit_gate_counter[arg0],
                                             arg1.register.name, arg1.index, qubit_gate_counter[arg1])
        qubit_gate_counter[arg0] += 1
        qubit_gate_counter[arg1] += 1
        # print(vertex.op.label,vertex_name,curr_node_id)
        if vertex_name not in node_name_ids and id(vertex) not in vertex_ids:
            node_name_ids[vertex_name] = curr_node_id
            id_node_names[curr_node_id] = vertex_name
            vertex_ids[id(vertex)] = curr_node_id
            curr_node_id += 1

    for u, v, _ in dag.edges():
        if u.type == 'op' and v.type == 'op':
            u_id = vertex_ids[id(u)]
            v_id = vertex_ids[id(v)]
            edges.append((u_id, v_id))
            
    n_vertices = dag.size()
    return n_vertices, edges, node_name_ids, id_node_names

def cuts_parser(cuts, circ):
    dag = circuit_to_dag(circ)
    positions = []
    for position in cuts:
        source, dest = position
        source_qargs = [(x.split(']')[0]+']',int(x.split(']')[1])) for x in source.split(' ')]
        dest_qargs = [(x.split(']')[0]+']',int(x.split(']')[1])) for x in dest.split(' ')]
        qubit_cut = []
        for source_qarg in source_qargs:
            source_qubit, source_multi_Q_gate_idx = source_qarg
            for dest_qarg in dest_qargs:
                dest_qubit, dest_multi_Q_gate_idx = dest_qarg
                if source_qubit==dest_qubit and dest_multi_Q_gate_idx == source_multi_Q_gate_idx+1:
                    qubit_cut.append(source_qubit)
        # if len(qubit_cut)>1:
        #     raise Exception('one cut is cutting on multiple qubits')
        for x in source.split(' '):
            if x.split(']')[0]+']' == qubit_cut[0]:
                source_idx = int(x.split(']')[1])
        for x in dest.split(' '):
            if x.split(']')[0]+']' == qubit_cut[0]:
                dest_idx = int(x.split(']')[1])
        multi_Q_gate_idx = max(source_idx, dest_idx)
        
        wire = None
        for qubit in circ.qubits:
            if qubit.register.name == qubit_cut[0].split('[')[0] and qubit.index == int(qubit_cut[0].split('[')[1].split(']')[0]):
                wire = qubit
        tmp = 0
        all_Q_gate_idx = None
        for gate_idx, gate in enumerate(list(dag.nodes_on_wire(wire=wire, only_ops=True))):
            if len(gate.qargs)>1:
                tmp += 1
                if tmp == multi_Q_gate_idx:
                    all_Q_gate_idx = gate_idx
        positions.append((wire, all_Q_gate_idx))
    positions = sorted(positions, reverse=True, key=lambda cut: cut[1])
    return positions

def subcircuits_parser(subcircuit_gates, circuit):
    '''
    Assign the single qubit gates to the closest two-qubit gates
    '''
    def calculate_distance_between_gate(gate_A, gate_B):
        if len(gate_A.split(' '))>=len(gate_B.split(' ')):
            tmp_gate = gate_A
            gate_A = gate_B
            gate_B = tmp_gate
        distance = float('inf')
        for qarg_A in gate_A.split(' '):
            qubit_A = qarg_A.split(']')[0]+']'
            qgate_A = int(qarg_A.split(']')[-1])
            for qarg_B in gate_B.split(' '):
                qubit_B = qarg_B.split(']')[0]+']'
                qgate_B = int(qarg_B.split(']')[-1])
                # print('%s gate %d --> %s gate %d'%(qubit_A,qgate_A,qubit_B,qgate_B))
                if qubit_A==qubit_B:
                    distance = min(distance,abs(qgate_B-qgate_A))
        # print('Distance from %s to %s = %f'%(gate_A,gate_B,distance))
        return distance

    dag = circuit_to_dag(circuit)
    qubit_allGate_depths = {x:0 for x in circuit.qubits}
    qubit_2qGate_depths = {x:0 for x in circuit.qubits}
    gate_depth_encodings = {}
    # print('Before translation :',subcircuit_gates,flush=True)
    for op_node in dag.topological_op_nodes():
        gate_depth_encoding = ''
        for qarg in op_node.qargs:
            gate_depth_encoding += '%s[%d]%d '%(qarg.register.name,qarg.index,qubit_allGate_depths[qarg])
        gate_depth_encoding = gate_depth_encoding[:-1]
        gate_depth_encodings[op_node] = gate_depth_encoding
        for qarg in op_node.qargs:
            qubit_allGate_depths[qarg] += 1
        if len(op_node.qargs)==2:
            MIP_gate_depth_encoding = ''
            for qarg in op_node.qargs:
                MIP_gate_depth_encoding += '%s[%d]%d '%(qarg.register.name,qarg.index,qubit_2qGate_depths[qarg])
                qubit_2qGate_depths[qarg] += 1
            MIP_gate_depth_encoding = MIP_gate_depth_encoding[:-1]
            # print('gate_depth_encoding = %s, MIP_gate_depth_encoding = %s'%(gate_depth_encoding,MIP_gate_depth_encoding))
            for subcircuit_idx in range(len(subcircuit_gates)):
                for gate_idx in range(len(subcircuit_gates[subcircuit_idx])):
                    if subcircuit_gates[subcircuit_idx][gate_idx]==MIP_gate_depth_encoding:
                        subcircuit_gates[subcircuit_idx][gate_idx]=gate_depth_encoding
                        break
    # print('After translation :',subcircuit_gates,flush=True)
    subcircuit_op_nodes = {x:[] for x in range(len(subcircuit_gates))}
    subcircuit_sizes = [0 for x in range(len(subcircuit_gates))]
    complete_path_map = {}
    for circuit_qubit in dag.qubits:
        complete_path_map[circuit_qubit] = []
        qubit_ops = dag.nodes_on_wire(wire=circuit_qubit,only_ops=True)
        for qubit_op_idx, qubit_op in enumerate(qubit_ops):
            gate_depth_encoding = gate_depth_encodings[qubit_op]
            nearest_subcircuit_idx = -1
            min_distance = float('inf')
            for subcircuit_idx in range(len(subcircuit_gates)):
                distance = float('inf')
                for gate in subcircuit_gates[subcircuit_idx]:
                    if len(gate.split(' '))==1:
                        # Do not compare against single qubit gates
                        continue
                    else:
                        distance = min(distance,calculate_distance_between_gate(gate_A=gate_depth_encoding, gate_B=gate))
                # print('Distance from %s to subcircuit %d = %f'%(gate_depth_encoding,subcircuit_idx,distance))
                if distance<min_distance:
                    min_distance = distance
                    nearest_subcircuit_idx = subcircuit_idx
            assert nearest_subcircuit_idx!=-1
            path_element = {'subcircuit_idx':nearest_subcircuit_idx,
            'subcircuit_qubit':subcircuit_sizes[nearest_subcircuit_idx]}
            if len(complete_path_map[circuit_qubit])==0 or nearest_subcircuit_idx!=complete_path_map[circuit_qubit][-1]['subcircuit_idx']:
                # print('{} op #{:d} {:s} encoding = {:s}'.format(circuit_qubit,qubit_op_idx,qubit_op.name,gate_depth_encoding),
                # 'belongs in subcircuit %d'%nearest_subcircuit_idx)
                complete_path_map[circuit_qubit].append(path_element)
                subcircuit_sizes[nearest_subcircuit_idx] += 1

            subcircuit_op_nodes[nearest_subcircuit_idx].append(qubit_op)
    for circuit_qubit in complete_path_map:
        # print(circuit_qubit,'-->')
        for path_element in complete_path_map[circuit_qubit]:
            path_element_qubit = QuantumRegister(size=subcircuit_sizes[path_element['subcircuit_idx']],name='q')[path_element['subcircuit_qubit']]
            path_element['subcircuit_qubit'] = path_element_qubit
            # print(path_element)
    subcircuits = generate_subcircuits(subcircuit_op_nodes=subcircuit_op_nodes, complete_path_map=complete_path_map, subcircuit_sizes=subcircuit_sizes, dag=dag)
    return subcircuits, complete_path_map

def generate_subcircuits(subcircuit_op_nodes, complete_path_map, subcircuit_sizes, dag):
    qubit_pointers = {x:0 for x in complete_path_map}
    subcircuits = [QuantumCircuit(x,name='q') for x in subcircuit_sizes]
    for op_node in dag.topological_op_nodes():
        subcircuit_idx = list(filter(lambda x:op_node in subcircuit_op_nodes[x],subcircuit_op_nodes.keys()))
        assert len(subcircuit_idx)==1
        subcircuit_idx = subcircuit_idx[0]
        # print('{} belongs in subcircuit {:d}'.format(op_node.qargs,subcircuit_idx))
        subcircuit_qargs = []
        for op_node_qarg in op_node.qargs:
            if complete_path_map[op_node_qarg][qubit_pointers[op_node_qarg]]['subcircuit_idx'] != subcircuit_idx:
                qubit_pointers[op_node_qarg] += 1
            path_element = complete_path_map[op_node_qarg][qubit_pointers[op_node_qarg]]
            assert path_element['subcircuit_idx']==subcircuit_idx
            subcircuit_qargs.append(path_element['subcircuit_qubit'])
        # print('-->',subcircuit_qargs)
        subcircuits[subcircuit_idx].append(instruction=op_node.op,qargs=subcircuit_qargs,cargs=None)
    return subcircuits

def circuit_stripping(circuit):
    # Remove all single qubit gates and barriers in the circuit
    dag = circuit_to_dag(circuit)
    stripped_dag = DAGCircuit()
    [stripped_dag.add_qreg(x) for x in circuit.qregs]
    for vertex in dag.topological_op_nodes():
        if len(vertex.qargs) == 2 and vertex.op.name!='barrier':
            stripped_dag.apply_operation_back(op=vertex.op, qargs=vertex.qargs)
    return dag_to_circuit(stripped_dag)

def cost_estimate(num_rho_qubits,num_O_qubits,num_d_qubits):
    num_cuts = sum(num_rho_qubits)
    num_rho_qubits = np.array(num_rho_qubits)
    num_O_qubits = np.array(num_O_qubits)
    num_d_qubits = np.array(num_d_qubits)
    num_effective_qubits = num_d_qubits - num_O_qubits
    num_effective_qubits, smart_order = zip(*sorted(zip(num_effective_qubits, range(len(num_d_qubits)))))
    reconstruction_cost = 0
    accumulated_kron_len = 1
    for counter, subcircuit_idx in enumerate(smart_order):
        rho = num_rho_qubits[subcircuit_idx]
        O = num_O_qubits[subcircuit_idx]
        d = num_d_qubits[subcircuit_idx]
        effective = d - O
        accumulated_kron_len *= 2**effective
        if counter > 0:
            reconstruction_cost += accumulated_kron_len
    reconstruction_cost *= 4**num_cuts
    return reconstruction_cost

def get_pairs(complete_path_map):
    O_rho_pairs = []
    for input_qubit in complete_path_map:
        path = complete_path_map[input_qubit]
        if len(path)>1:
            for path_ctr, item in enumerate(path[:-1]):
                O_qubit_tuple = item
                rho_qubit_tuple = path[path_ctr+1]
                O_rho_pairs.append((O_qubit_tuple, rho_qubit_tuple))
    return O_rho_pairs

def get_counter(subcircuits, O_rho_pairs):
    counter = {}
    for subcircuit_idx, subcircuit in enumerate(subcircuits):
        counter[subcircuit_idx] = {'effective':subcircuit.num_qubits,'rho':0,'O':0,'d':subcircuit.num_qubits,
        'depth':subcircuit.depth(),
        'size':subcircuit.size()}
    for pair in O_rho_pairs:
        O_qubit, rho_qubit = pair
        counter[O_qubit['subcircuit_idx']]['effective'] -= 1
        counter[O_qubit['subcircuit_idx']]['O'] += 1
        counter[rho_qubit['subcircuit_idx']]['rho'] += 1
    return counter

def find_cuts(circuit, max_subcircuit_qubit, max_cuts, num_subcircuits, verbose):
    stripped_circ = circuit_stripping(circuit=circuit)
    n_vertices, edges, vertex_ids, id_vertices = read_circ(circuit=stripped_circ)
    num_qubits = circuit.num_qubits
    cut_solution = {}
    min_postprocessing_cost = float('inf')
    
    for num_subcircuit in num_subcircuits:
        if num_subcircuit*max_subcircuit_qubit-(num_subcircuit-1)<num_qubits \
            or num_subcircuit>num_qubits \
            or max_cuts+1<num_subcircuit:
            if verbose:
                print('%d-qubit circuit, %d subcircuits, max size %d, max cuts %d: IMPOSSIBLE'%(
                    num_qubits,num_subcircuit,max_subcircuit_qubit,max_cuts))
            continue
        kwargs = dict(n_vertices=n_vertices,
                    edges=edges,
                    vertex_ids=vertex_ids,
                    id_vertices=id_vertices,
                    num_subcircuit=num_subcircuit,
                    max_subcircuit_qubit=max_subcircuit_qubit,
                    num_qubits=num_qubits,
                    max_cuts=max_cuts)

        mip_model = MIP_Model(**kwargs)
        feasible = mip_model.solve(min_postprocessing_cost)
        if not feasible:
            if verbose:
                print('%d-qubit circuit, %d subcircuits, max size %d, max cuts %d: NO SOLUTIONS'%(
                    num_qubits,num_subcircuit,max_subcircuit_qubit,max_cuts))
            continue
        else:
            min_objective = mip_model.objective
            positions = cuts_parser(mip_model.cut_edges, circuit)
            subcircuits, complete_path_map = subcircuits_parser(subcircuit_gates=mip_model.subcircuits_vertices, circuit=circuit)
            num_rho_qubits = []
            num_O_qubits = []
            num_d_qubits = []
            for i in range(num_subcircuit):
                subcircuit_rho_qubits = mip_model.model.getVarByName('subcircuit_rho_qubits_%d'%i)
                subcircuit_O_qubits = mip_model.model.getVarByName('subcircuit_O_qubits_%d'%i)
                subcircuit_d = mip_model.model.getVarByName('subcircuit_d_%d'%i)
                num_rho_qubits.append(subcircuit_rho_qubits.X)
                num_O_qubits.append(subcircuit_O_qubits.X)
                num_d_qubits.append(subcircuit_d.X)
            
            O_rho_pairs = get_pairs(complete_path_map=complete_path_map)
            counter = get_counter(subcircuits=subcircuits, O_rho_pairs=O_rho_pairs)

            reconstruction_cost = cost_estimate(num_rho_qubits,num_O_qubits,num_d_qubits)
            if verbose:
                print('-'*20)
                print('MIP trial:')
                # print('node count:', self.node_count)
                # print('%d vertices %d edges graph. Max qubit = %d'%
                # (self.n_vertices, self.n_edges, self.max_subcircuit_qubit))
                print('%d subcircuits, %d cuts'%(num_subcircuit,len(mip_model.cut_edges)))

                for subcircuit_idx in range(num_subcircuit):
                    print('subcircuit %d'%subcircuit_idx)
                    print('\u03C1 qubits = %d, O qubits = %d, width = %d, effective = %d, depth = %d, size = %d' % 
                    (counter[subcircuit_idx]['rho'],
                    counter[subcircuit_idx]['O'],
                    counter[subcircuit_idx]['d'],
                    counter[subcircuit_idx]['effective'],
                    counter[subcircuit_idx]['depth'],
                    counter[subcircuit_idx]['size']))
                    print(subcircuits[subcircuit_idx])

                print('Model objective value = %.2e'%(mip_model.objective))
                print('MIP runtime:', mip_model.runtime)

                if (mip_model.optimal):
                    print('OPTIMAL, MIP gap =',mip_model.mip_gap)
                else:
                    print('NOT OPTIMAL, MIP gap =',mip_model.mip_gap)
                print('Estimated postprocessing cost = %.3e'%reconstruction_cost,flush=True)
                print('-'*20)

            if reconstruction_cost < min_postprocessing_cost:
                min_postprocessing_cost = reconstruction_cost
                cut_solution = {
                'circuit':circuit,
                'max_subcircuit_qubit':max_subcircuit_qubit,
                'subcircuits':subcircuits,
                'complete_path_map':complete_path_map,
                'searcher_time':mip_model.runtime,
                'num_rho_qubits':num_rho_qubits,
                'num_O_qubits':num_O_qubits,
                'num_d_qubits':num_d_qubits,
                'objective':mip_model.objective,
                'positions':positions,
                'counter':counter,
                'cost_estimate':reconstruction_cost}
    return cut_solution