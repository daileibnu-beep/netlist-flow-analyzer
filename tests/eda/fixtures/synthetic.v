// Synthetic fixture written for this project; no PDK or external netlist.
module tx(q);
output [3:0] q;
DRV d0 (.Y(q[0]));
DRV d1 (.Y(q[1]));
DRV d2 (.Y(q[2]));
DRV d3 (.Y(q[3]));
endmodule
module rx(d);
input [0:3] d;
LOAD l0 (.A(d[0]));
LOAD l1 (.A(d[1]));
LOAD l2 (.A(d[2]));
LOAD l3 (.A(d[3]));
endmodule
module branch(o,i);
output [3:0] o;
input [3:0] i;
tx producer (.q(o));
rx consumer (.d(i));
endmodule
module empty_box(a,y);
input a;
output y;
endmodule
module pass_only(a,y);
input a;
output y;
assign y = a;
endmodule
module scalar_bracket(\a[0] );
input \a[0] ;
LOAD keep (.A(\a[0] ));
endmodule
module bidir(pad);
inout pad;
PADIO keep (.PAD(pad));
endmodule
module fixture_top(ext,external_out,pad);
input [3:0] ext;
output [3:0] external_out;
inout pad;
wire [3:0] n0;
wire [3:0] n1;
wire [3:0] n2;
wire [3:0] n3;
wire [3:0] alias_vec;
wire alias_a;
wire alias_b;
wire empty_y;
assign external_out = n0;
branch \gen[2].u_group  (.o(n0), .i(ext));
branch sibling (.o(n1), .i({n0[0],n0[1],n0[2],n0[3]}));
branch \path/with~mark  (.o(n2), .i(n1));
branch \other.path  (.o(n3), .i(n2));
rx sink (.d(n1));
rx constants (.d(4'b1010));
rx dangling (.d());
scalar_bracket scalar (.\a[0] (n0[0]));
bidir io (.pad(pad));
empty_box empty (.a(n0[0]), .y(empty_y));
tx alias_source (.q(alias_vec));
assign alias_a = alias_vec[0];
pass_only passthrough (.a(alias_a), .y(alias_b));
rx alias_target (.d({3'b000, alias_b}));
endmodule
