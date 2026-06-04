module clk_probe #(
    parameter integer PIN_COUNT = 4
)
(
    input  wire clk_in,
    output wire [PIN_COUNT-1:0] pin_out,
    output wire [1:0] led
);

reg [31:0] cnt = 32'd0;

always @(posedge clk_in) begin
    cnt <= cnt + 1'b1;
end

localparam [PIN_COUNT-1:0] PIN_MASK = 4'b0100;

assign pin_out = PIN_MASK;
assign led[0] = cnt[27];
assign led[1] = 1'b1;

endmodule
